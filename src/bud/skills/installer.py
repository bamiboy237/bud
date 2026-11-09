"""The released skills CLI, isolated from the owner's shell and agent configuration."""

import asyncio
import fcntl
import hashlib
import os
import platform
import re
import signal
import tarfile
import tempfile
from contextlib import suppress
from importlib import resources
from pathlib import Path

import httpx
from pydantic_ai import ModelRetry

NODE_VERSION = "22.20.0"
NODE_SHA256 = {
    ("Linux", "x86_64"): "eeaccb0378b79406f2208e8b37a62479c70595e20be6b659125eb77dd1ab2a29",
    ("Linux", "aarch64"): "4181609e03dcb9880e7e5bf956061ecc0503c77a480c6631d868cb1f65a2c7dd",
    ("Darwin", "arm64"): "cc04a76a09f79290194c0646f48fec40354d88969bec467789a5d55dd097f949",
    ("Darwin", "x86_64"): "00df9c5df3e4ec6848c26b70fb47bf96492f342f4bed6b17f12d99b3a45eeecc",
}
SOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,38}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


class SkillError(ModelRetry):
    pass


class Installer:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _node(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / "setup.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            host = (platform.system(), platform.machine())
            digest = NODE_SHA256.get(host)
            if digest is None:
                raise SkillError("Skill installation currently supports macOS and Linux x64/arm64.")
            os_name = "darwin" if host[0] == "Darwin" else "linux"
            arch = "arm64" if host[1] in {"arm64", "aarch64"} else "x64"
            name = f"node-v{NODE_VERSION}-{os_name}-{arch}"
            node = self.root / name / "bin" / "node"
            if node.is_file():
                return node
            with tempfile.TemporaryDirectory(dir=self.root) as temporary:
                folder = Path(temporary)
                archive = folder / "node.tar.gz"
                checksum = hashlib.sha256()
                size = 0
                with (
                    httpx.stream(
                        "GET",
                        f"https://nodejs.org/dist/v{NODE_VERSION}/{name}.tar.gz",
                        timeout=60,
                        trust_env=False,
                    ) as response,
                    archive.open("wb") as output,
                ):
                    response.raise_for_status()
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 80 * 1024 * 1024:
                            raise SkillError("Node download exceeded its size limit.")
                        checksum.update(chunk)
                        output.write(chunk)
                if checksum.hexdigest() != digest:
                    raise SkillError("Node download failed its pinned SHA-256 check.")
                with tarfile.open(archive) as package:
                    package.extractall(folder, filter="data")
                (folder / name).rename(self.root / name)
            return node

    async def run(self, args: list[str], cwd: Path) -> str:
        node = await asyncio.to_thread(self._node)
        for directory in ("home", "npm"):
            (self.root / directory).mkdir(exist_ok=True, mode=0o700)
        env = {
            "PATH": f"{node.parent}{os.pathsep}{os.defpath}",
            "HOME": str(self.root / "home"),
            "LANG": "C.UTF-8",
            "npm_config_cache": str(self.root / "npm"),
            "npm_config_ignore_scripts": "true",
            "npm_config_audit": "false",
            "npm_config_fund": "false",
            "DISABLE_TELEMETRY": "1",
            "DO_NOT_TRACK": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        manifest = resources.files("bud.skills").joinpath("runtime")
        lock_bytes = manifest.joinpath("package-lock.json").read_bytes()
        generation = hashlib.sha256(lock_bytes).hexdigest()[:12]
        target = self.root / f"skills-{generation}"
        cli = target / "node_modules/skills/bin/cli.mjs"
        with (self.root / "installer.lock").open("a") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.1)
            if not cli.is_file():
                if target.exists():
                    raise SkillError("The managed skill installer cache is incomplete.")
                with tempfile.TemporaryDirectory(dir=self.root) as temporary:
                    stage = Path(temporary) / "cli"
                    stage.mkdir()
                    (stage / "package.json").write_bytes(
                        manifest.joinpath("package.json").read_bytes()
                    )
                    (stage / "package-lock.json").write_bytes(lock_bytes)
                    await self._execute(
                        [
                            str(node),
                            str(node.parent.parent / "lib/node_modules/npm/bin/npm-cli.js"),
                            "ci",
                            "--ignore-scripts",
                            "--no-audit",
                            "--no-fund",
                        ],
                        stage,
                        env,
                    )
                    stage.rename(target)
        return await self._execute([str(node), str(cli), *args], cwd, env)

    @staticmethod
    async def _execute(argv: list[str], cwd: Path, env: dict[str, str]) -> str:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            async with asyncio.timeout(180):
                output = bytearray()
                while chunk := await process.stdout.read(16_384):
                    output.extend(chunk)
                    if len(output) > 262_144:
                        raise SkillError("Skill command exceeded its output limit.")
                await process.wait()
            if process.returncode:
                raise SkillError(
                    "The skills CLI could not complete this request. Check the source, "
                    "selected skill and network access."
                )
            return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output.decode(errors="replace"))
        except TimeoutError:
            raise SkillError("Skill command timed out; nothing was activated.") from None
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()

    async def prepare(self, source: str, skill: str, destination: Path) -> tuple[Path, str]:
        if not SOURCE.fullmatch(source) or ".." in source:
            raise SkillError("Use a public GitHub source in owner/repository form.")
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            response = await client.get(
                f"https://api.github.com/repos/{source}/commits/HEAD",
                headers={"Accept": "application/vnd.github+json"},
            )
            if response.status_code != 200:
                raise SkillError("Cannot resolve that public GitHub repository.")
            revision = response.json().get("sha", "")
        if not re.fullmatch(r"[a-f0-9]{40}", revision):
            raise SkillError("Source returned an invalid revision.")
        await self.run(
            [
                "add",
                f"https://github.com/{source}/tree/{revision}",
                "--skill",
                skill,
                "--agent",
                "universal",
                "--copy",
                "--yes",
            ],
            destination,
        )
        return destination / ".agents" / "skills" / skill, revision
