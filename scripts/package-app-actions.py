"""Copy the fixed scripts and allowed app names into the signed Mac bundle."""

import ast
import json
import sys
from pathlib import Path

source, destination = map(Path, sys.argv[1:])
values = {
    item.targets[0].id: item.value
    for item in ast.parse(source.read_text()).body
    if isinstance(item, ast.Assign) and isinstance(item.targets[0], ast.Name)
}
scripts = values["MAC_SCRIPTS"]
assert isinstance(scripts, ast.Dict)
destination.mkdir(exist_ok=True)
for index, key in enumerate(scripts.keys):
    value = scripts.values[index]
    assert isinstance(key, ast.Constant) and isinstance(value, ast.Name)
    script = ast.literal_eval(values[value.id])
    (destination / f"{key.value}.applescript").write_text(script)
(destination / "apps.json").write_text(json.dumps(ast.literal_eval(values["ALLOWED_APPS"])))
quiet = ast.literal_eval(values["QUIET_TYPES"])
asked_types = values["ASKED_TYPES"]
assert isinstance(asked_types, ast.Call)
asked = ast.literal_eval(asked_types.args[0])
(destination / "documents.json").write_text(json.dumps({**quiet, **dict.fromkeys(asked, "")}))
