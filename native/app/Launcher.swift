//
//  Launcher.swift — Planner native macOS shell (WKWebView + embedded Python server).
//
import AppKit
import Darwin
import Foundation
import WebKit

private let kServerHost = "127.0.0.1"
private let kServerPort: UInt16 = 8765
private let kServerURLString = "http://127.0.0.1:8765/"
private let kServerLogPath = "/tmp/planner_server.log"
private let kWindowAutosaveName = "PlannerMainWindow"
private let kStartupTimeout: TimeInterval = 10.0
private let kConnectTimeout: TimeInterval = 0.4

private func tcpPortOpen(host: String, port: UInt16, timeout: TimeInterval) -> Bool {
    let descriptor = socket(AF_INET, SOCK_STREAM, 0)
    if descriptor < 0 { return false }
    defer { close(descriptor) }

    var address = sockaddr_in()
    address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
    address.sin_family = sa_family_t(AF_INET)
    address.sin_port = port.bigEndian
    address.sin_addr = in_addr(s_addr: host.withCString { inet_addr($0) })

    let flags = fcntl(descriptor, F_GETFL, 0)
    if flags >= 0 {
        _ = fcntl(descriptor, F_SETFL, flags | O_NONBLOCK)
    }

    let connectResult = withUnsafePointer(to: &address) { pointer -> Int32 in
        pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { socketAddress in
            connect(descriptor, socketAddress, socklen_t(MemoryLayout<sockaddr_in>.size))
        }
    }
    if connectResult == 0 { return true }
    if errno != EINPROGRESS { return false }

    var pollDescriptor = pollfd(fd: descriptor, events: Int16(POLLOUT), revents: 0)
    let milliseconds = Int32(max(1.0, (timeout * 1000.0).rounded()))
    let ready = poll(&pollDescriptor, 1, milliseconds)
    if ready <= 0 { return false }

    var socketError: Int32 = 0
    var length = socklen_t(MemoryLayout<Int32>.size)
    if getsockopt(descriptor, SOL_SOCKET, SO_ERROR, &socketError, &length) != 0 {
        return false
    }
    return socketError == 0
}

final class PlannerAppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKScriptMessageHandler, WKUIDelegate {
    private var window: NSWindow?
    private var webView: WKWebView?
    private var serverProcess: Process?
    private var logHandle: FileHandle?
    private var startupError: String?
    private var startupGeneration = 0
    private var isTerminating = false

    private func stopExistingServers() {
        if let process = serverProcess, process.isRunning {
            process.terminate()
            process.waitUntilExit()
        }
        serverProcess = nil
        let killer = Process()
        killer.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        killer.arguments = ["-f", "planner.cli web"]
        try? killer.run()
        killer.waitUntilExit()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        stopExistingServers()
        buildMenu()
        buildWindow()
        NSApp.activate(ignoringOtherApps: true)
        bootstrap()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        isTerminating = true
        stopExistingServers()
        if let handle = logHandle {
            try? handle.close()
        }
    }

    private func buildWindow() {
        let configuration = WKWebViewConfiguration()
        configuration.defaultWebpagePreferences.allowsContentJavaScript = true
        configuration.userContentController.add(self, name: "planner")

        let webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 1280, height: 900), configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        self.webView = webView

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 900),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Planner"
        window.minSize = NSSize(width: 900, height: 600)
        window.isReleasedWhenClosed = false
        window.tabbingMode = .disallowed
        window.backgroundColor = NSColor(calibratedRed: 0.043, green: 0.059, blue: 0.078, alpha: 1.0)
        window.contentView = webView
        self.window = window

        let restored = window.setFrameUsingName(kWindowAutosaveName)
        _ = window.setFrameAutosaveName(kWindowAutosaveName)
        if !restored { window.center() }
        window.makeKeyAndOrderFront(nil)
    }

    private func bootstrap() {
        startupGeneration += 1
        let generation = startupGeneration
        startupError = nil

        if tcpPortOpen(host: kServerHost, port: kServerPort, timeout: kConnectTimeout) {
            loadPlanner()
            return
        }

        let launched = startServerIfNeeded()
        if !launched {
            return
        }

        let deadline = Date().addingTimeInterval(kStartupTimeout)
        pollForServer(deadline: deadline) { [weak self] reachable in
            guard let self = self, generation == self.startupGeneration else { return }
            if reachable {
                self.loadPlanner()
            }
        }
    }

    private func resolveProjectRoot() -> String {
        let fm = FileManager.default
        let home = fm.homeDirectoryForCurrentUser.path
        var candidates: [String] = []
        if let saved = UserDefaults.standard.string(forKey: "PlannerProjectRoot"), !saved.isEmpty {
            candidates.append(saved)
        }
        if let configured = Bundle.main.object(forInfoDictionaryKey: "ProjectRoot") as? String, !configured.isEmpty {
            candidates.append(configured)
        }
        candidates.append(contentsOf: [
            "\(home)/Desktop/personal-planner",
            "\(home)/Desktop/Planner",
            "\(home)/personal-planner",
            "\(home)/Planner",
            "\(home)/Projects/personal-planner",
            "\(home)/Documents/personal-planner",
            "\(home)/Documents/GitHub/personal-planner"
        ])
        for cand in candidates {
            if fm.fileExists(atPath: "\(cand)/planner/cli.py") {
                UserDefaults.standard.set(cand, forKey: "PlannerProjectRoot")
                return cand
            }
        }
        // Prompt user to locate the repository folder if moved to an unknown location
        let panel = NSOpenPanel()
        panel.title = "Locate Planner Repository Folder"
        panel.message = "Select the 'personal-planner' folder containing the Planner source code:"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let url = panel.url {
            let chosen = url.path
            if fm.fileExists(atPath: "\(chosen)/planner/cli.py") {
                UserDefaults.standard.set(chosen, forKey: "PlannerProjectRoot")
                return chosen
            }
        }
        return candidates.first ?? "\(home)/Desktop/personal-planner"
    }

    private func startServerIfNeeded() -> Bool {
        if let process = serverProcess, process.isRunning {
            return true
        }
        let projectRoot = resolveProjectRoot()

        if !FileManager.default.fileExists(atPath: kServerLogPath) {
            _ = FileManager.default.createFile(atPath: kServerLogPath, contents: nil, attributes: nil)
        }
        guard let handle = FileHandle(forWritingAtPath: kServerLogPath) else { return false }
        _ = try? handle.seekToEnd()

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = ["-m", "planner.cli", "web", "--port", String(kServerPort)]
        process.currentDirectoryURL = URL(fileURLWithPath: projectRoot, isDirectory: true)

        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        process.environment = environment
        process.standardOutput = handle
        process.standardError = handle

        do {
            try process.run()
        } catch {
            return false
        }

        serverProcess = process
        logHandle = handle
        return true
    }

    private func pollForServer(deadline: Date, completion: @escaping (Bool) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            while Date() < deadline {
                if tcpPortOpen(host: kServerHost, port: kServerPort, timeout: kConnectTimeout) {
                    DispatchQueue.main.async { completion(true) }
                    return
                }
                Thread.sleep(forTimeInterval: 0.2)
            }
            DispatchQueue.main.async { completion(false) }
        }
    }

    private func loadPlanner() {
        guard let url = URL(string: kServerURLString) else { return }
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 20)
        request.setValue("Planner/2.0 (macOS)", forHTTPHeaderField: "User-Agent")
        webView?.load(request)
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {}

    private func buildMenu() {
        let mainMenu = NSMenu()

        let appMenuItem = NSMenuItem(title: "Planner", action: nil, keyEquivalent: "")
        let appMenu = NSMenu(title: "Planner")
        appMenuItem.submenu = appMenu
        mainMenu.addItem(appMenuItem)

        appMenu.addItem(NSMenuItem(title: "About Planner", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: ""))
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(NSMenuItem(title: "Quit Planner", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))

        let fileItem = NSMenuItem(title: "File", action: nil, keyEquivalent: "")
        let fileMenu = NSMenu(title: "File")
        fileItem.submenu = fileMenu
        mainMenu.addItem(fileItem)

        fileMenu.addItem(NSMenuItem(title: "Reload", action: #selector(reloadPage(_:)), keyEquivalent: "r"))
        fileMenu.addItem(NSMenuItem(title: "Open in Browser", action: #selector(openInBrowser(_:)), keyEquivalent: "O"))

        let editMenuItem = NSMenuItem(title: "Edit", action: nil, keyEquivalent: "")
        let editMenu = NSMenu(title: "Edit")
        editMenuItem.submenu = editMenu
        mainMenu.addItem(editMenuItem)

        editMenu.addItem(NSMenuItem(title: "Undo", action: Selector(("undo:")), keyEquivalent: "z"))
        editMenu.addItem(NSMenuItem(title: "Redo", action: Selector(("redo:")), keyEquivalent: "Z"))
        editMenu.addItem(NSMenuItem.separator())
        editMenu.addItem(NSMenuItem(title: "Cut", action: Selector(("cut:")), keyEquivalent: "x"))
        editMenu.addItem(NSMenuItem(title: "Copy", action: Selector(("copy:")), keyEquivalent: "c"))
        editMenu.addItem(NSMenuItem(title: "Paste", action: Selector(("paste:")), keyEquivalent: "v"))
        editMenu.addItem(NSMenuItem(title: "Select All", action: Selector(("selectAll:")), keyEquivalent: "a"))

        NSApp.mainMenu = mainMenu
    }

    @objc private func reloadPage(_ sender: Any?) {
        bootstrap()
    }

    @objc private func openInBrowser(_ sender: Any?) {
        guard let url = URL(string: kServerURLString) else { return }
        NSWorkspace.shared.open(url)
    }

    // MARK: - WKUIDelegate
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = "Planner"
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        if let window = self.window {
            alert.beginSheetModal(for: window) { _ in completionHandler() }
        } else {
            alert.runModal()
            completionHandler()
        }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = "Planner"
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        alert.alertStyle = .warning
        if let window = self.window {
            alert.beginSheetModal(for: window) { res in
                completionHandler(res == .alertFirstButtonReturn)
            }
        } else {
            let res = alert.runModal()
            completionHandler(res == .alertFirstButtonReturn)
        }
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        let alert = NSAlert()
        alert.messageText = "Planner"
        alert.informativeText = prompt
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
        input.stringValue = defaultText ?? ""
        alert.accessoryView = input
        if let window = self.window {
            alert.beginSheetModal(for: window) { res in
                completionHandler(res == .alertFirstButtonReturn ? input.stringValue : nil)
            }
        } else {
            let res = alert.runModal()
            completionHandler(res == .alertFirstButtonReturn ? input.stringValue : nil)
        }
    }
}

let app = NSApplication.shared
let delegate = PlannerAppDelegate()
app.delegate = delegate
_ = app.setActivationPolicy(.regular)
app.run()
