import AppKit
import SwiftUI

struct SafePreviewURL {
    let value: URL

    init?(_ raw: String?) {
        guard let raw, let url = URL(string: raw),
              url.scheme?.lowercased() == "https", let host = url.host(), !host.isEmpty,
              url.user() == nil, url.password() == nil else { return nil }
        value = url
    }
}

struct WebLinkCard: View {
    let url: URL
    let title: String

    var body: some View {
        Link(destination: url) {
            HStack(spacing: 10) {
                Image(systemName: "link").foregroundStyle(Paper.accent)
                VStack(alignment: .leading, spacing: 4) {
                    Text(title.isEmpty ? url.absoluteString : title)
                        .font(Paper.Typography.callout).lineLimit(2)
                    Text(url.host() ?? url.absoluteString).font(Paper.Typography.ledgerMeta)
                        .foregroundStyle(Paper.inkSecondary).lineLimit(1)
                }
                Spacer(minLength: 10)
                Image(systemName: "arrow.up.right").font(.caption2).foregroundStyle(Paper.inkSecondary)
            }
            .padding(10)
            .frame(maxWidth: 420, alignment: .leading)
            .background(Paper.elevated, in: RoundedRectangle(cornerRadius: Paper.radiusMD))
        }
        .buttonStyle(.plain)
    }
}

struct WebImageCard: View {
    let url: URL
    let title: String
    @State private var requested = false

    var body: some View {
        if requested {
            AsyncImage(url: url) { phase in
                switch phase {
                case .success(let image):
                    image.resizable().scaledToFit()
                        .frame(maxWidth: 480, maxHeight: 300)
                        .clipShape(RoundedRectangle(cornerRadius: Paper.radiusMD))
                        .accessibilityLabel(title.isEmpty ? "Image" : title)
                case .failure:
                    WebLinkCard(url: url, title: title.isEmpty ? "Open image" : title)
                default:
                    ProgressView().controlSize(.small).accessibilityLabel("Loading image")
                }
            }
        } else {
            Button { requested = true } label: {
                HStack(spacing: 9) {
                    Image(systemName: "photo").foregroundStyle(Paper.accent)
                    VStack(alignment: .leading, spacing: 4) {
                        Text(title.isEmpty ? "Show image" : title).font(Paper.Typography.callout)
                        Text("Load from \(url.host() ?? "source")").font(Paper.Typography.ledgerMeta)
                            .foregroundStyle(Paper.inkSecondary)
                    }
                }
                .padding(10)
                .background(Paper.elevated, in: RoundedRectangle(cornerRadius: Paper.radiusMD))
            }
            .buttonStyle(.plain)
            .help("Load image")
        }
    }
}
