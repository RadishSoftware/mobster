// pdf: renders a self-contained HTML file with WebKit, off screen, into a paginated PDF with page numbers, or a PNG of it.
//   pdf <in.html> <out.pdf> [--title T]      Letter pages, 0.6in margins, "Page N of M" centred in the footer
//   pdf <in.html> <out.png> --png [--width W]  one image of the whole page at 2x, W CSS pixels wide (default 720)
// No window is ever shown and no Dock icon appears (activation policy prohibited). Network loads are refused: the HTML
// carries its fonts and images as data: URLs. Exit 0 on success; 1 with one line on stderr otherwise.
import AppKit
import WebKit

let args = CommandLine.arguments
guard args.count >= 3 else { FileHandle.standardError.write("usage: pdf in.html out.(pdf|png) [--png] [--width W] [--title T]\n".data(using: .utf8)!); exit(1) }
let input = URL(fileURLWithPath: args[1]), output = URL(fileURLWithPath: args[2])
let png = args.contains("--png")
func value(_ flag: String) -> String? { if let i = args.firstIndex(of: flag), i + 1 < args.count { return args[i + 1] }; return nil }
let width = CGFloat(Double(value("--width") ?? "720") ?? 720)
let title = value("--title") ?? ""

func fail(_ message: String) -> Never { FileHandle.standardError.write((message + "\n").data(using: .utf8)!); exit(1) }

final class Blocker: NSObject, WKNavigationDelegate {
  var done: ((Error?) -> Void)?
  func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
    let scheme = action.request.url?.scheme ?? ""
    decisionHandler(scheme == "file" || scheme == "about" || scheme == "data" ? .allow : .cancel)
  }
  func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { done?(nil) }
  func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { done?(error) }
  func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { done?(error) }
}

// Page numbers: WebKit asks its UI delegate for a footer on each printed page (the same hook Safari uses).
final class Footer: NSObject, WKUIDelegate {
  var pages = 0
  @objc func _webViewFooterHeight(_ webView: WKWebView) -> CGFloat { 28 }
  @objc func _webViewHeaderHeight(_ webView: WKWebView) -> CGFloat { 0 }
  @objc func _webView(_ webView: WKWebView, drawFooterInRect rect: NSRect, forPageWithTitle pageTitle: String, URL: String) {
    guard let op = NSPrintOperation.current else { return }
    let page = op.currentPage
    let total = op.pageRange.length
    let text = total > 0 ? "\(page) of \(total)" : "\(page)"
    let style = NSMutableParagraphStyle(); style.alignment = .center
    let attrs: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 8.5), .foregroundColor: NSColor(white: 0.45, alpha: 1), .paragraphStyle: style]
    (text as NSString).draw(in: NSRect(x: rect.minX, y: rect.minY + 6, width: rect.width, height: 14), withAttributes: attrs)
  }
}

let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
let config = WKWebViewConfiguration()
config.websiteDataStore = .nonPersistent()
let frame = NSRect(x: 0, y: 0, width: png ? width : 612, height: 800)
let view = WKWebView(frame: frame, configuration: config)
let window = NSWindow(contentRect: frame, styleMask: [.borderless], backing: .buffered, defer: false)
window.contentView = view  // never ordered front: printing and snapshots need a window, not a visible one
let blocker = Blocker(), footer = Footer()
view.navigationDelegate = blocker
view.uiDelegate = footer

blocker.done = { error in
  if let error = error { fail("Mobster couldn't lay out the document: \(error.localizedDescription)") }
  if png {
    view.evaluateJavaScript("Math.ceil(document.body.getBoundingClientRect().bottom + parseFloat(getComputedStyle(document.body).marginBottom || 0))") { result, _ in
      let height = CGFloat((result as? NSNumber)?.doubleValue ?? 800)
      view.setFrameSize(NSSize(width: width, height: height)); window.setContentSize(NSSize(width: width, height: height))
      DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) {
        let shot = WKSnapshotConfiguration(); shot.rect = NSRect(x: 0, y: 0, width: width, height: height); shot.snapshotWidth = NSNumber(value: Double(width * 2))
        view.takeSnapshot(with: shot) { image, error in
          guard let image = image, let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
                let data = rep.representation(using: .png, properties: [:]) else { fail("Mobster couldn't draw the image: \(error?.localizedDescription ?? "no image")") }
          do { try data.write(to: output) } catch { fail("Mobster couldn't save the image: \(error.localizedDescription)") }
          exit(0)
        }
      }
    }
    return
  }
  let info = NSPrintInfo(dictionary: [.jobSavingURL: output])
  info.jobDisposition = .save
  info.paperSize = NSSize(width: 612, height: 792)
  info.topMargin = 40; info.bottomMargin = 30; info.leftMargin = 43; info.rightMargin = 43
  info.horizontalPagination = .fit; info.verticalPagination = .automatic
  info.isHorizontallyCentered = false; info.isVerticallyCentered = false
  info.dictionary()[NSPrintInfo.AttributeKey.headerAndFooter] = false
  let op = view.printOperation(with: info)
  op.showsPrintPanel = false; op.showsProgressPanel = false
  op.jobTitle = title
  op.view?.frame = NSRect(x: 0, y: 0, width: 526, height: 800)
  op.runModal(for: window, delegate: Done.shared, didRun: #selector(Done.printed(_:success:contextInfo:)), contextInfo: nil)
}

final class Done: NSObject {
  static let shared = Done()
  @objc func printed(_ op: NSPrintOperation, success: Bool, contextInfo: UnsafeMutableRawPointer?) {
    if !success { fail("Mobster couldn't make the PDF.") }
    exit(0)
  }
}

view.loadFileURL(input, allowingReadAccessTo: input.deletingLastPathComponent())
DispatchQueue.main.asyncAfter(deadline: .now() + 30) { fail("Mobster couldn't make the document in 30 seconds.") }
app.run()
