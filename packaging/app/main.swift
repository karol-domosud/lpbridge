// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Karol Domosud

import AppKit
import QuartzCore
import SwiftUI

// Avoid the SwiftUIMacros plugin requirement in CLT-only builds.
typealias ViewState = SwiftUI.State

enum AppStyle {
    static let inset: CGFloat = 28
    static let topInset: CGFloat = 38
    static let splashOffset: CGFloat = 0
    static let transitionDuration: TimeInterval = 0.35
    static let accent = Color(red: 0.18, green: 0.40, blue: 0.92)
    static let surface = Color(nsColor: .controlBackgroundColor)
    static let border = Color.primary.opacity(0.08)
}

enum AppWindowLayout: Hashable {
    case splash, list

    var size: NSSize {
        // The splash pages (waking up, error, empty library) are one column of
        // icon, copy and a button: a square frames that better than a wide box.
        self == .splash ? NSSize(width: 340, height: 360) : NSSize(width: 500, height: 460)
    }
    var minimumSize: NSSize {
        self == .splash ? NSSize(width: 320, height: 340) : NSSize(width: 500, height: 460)
    }
}

extension AppModel {
    var windowLayout: AppWindowLayout {
        switch page {
        case .discovery: return .list
        case .library: return printers.isEmpty ? .splash : .list
        default: return .splash
        }
    }
}

struct PrinterArtwork: View {
    var success = false
    var body: some View {
        Image(systemName: success ? "checkmark" : "printer.fill")
            .font(.system(size: 34, weight: .medium))
            .foregroundStyle(success ? Color.green : Color.accentColor)
            .frame(width: 80, height: 80)
            .background((success ? Color.green : Color.accentColor).opacity(0.09),
                        in: RoundedRectangle(cornerRadius: 22, style: .continuous))
            .accessibilityHidden(true)
    }
}

struct RootView: View {
    @ObservedObject var model: AppModel
    var onWindowLayoutChange: (AppWindowLayout) -> Void = { _ in }
    @ViewState private var removal: Printer?
    @ViewState private var chosenDriver: [String: String] = [:]
    @ViewState private var driverDropTargeted = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Group {
            switch model.page {
            case .welcome: welcome
            case .preparing: preparing
            case .discovery: discovery
            case .library: library
            case .success: success
            }
        }
        .frame(minWidth: AppWindowLayout.splash.minimumSize.width, maxWidth: .infinity,
               minHeight: AppWindowLayout.splash.minimumSize.height, maxHeight: .infinity)
        .background(Color(nsColor: .windowBackgroundColor))
        .tint(AppStyle.accent).accentColor(AppStyle.accent)
        .ignoresSafeArea(.container, edges: .top)
        .animation(reduceMotion ? nil : .easeInOut(duration: AppStyle.transitionDuration), value: model.page)
        .onChange(of: model.windowLayout) { onWindowLayoutChange($0) }
        .task { await model.connectIfRunning() }
        .alert("Remove \(removal?.name ?? "printer")?", isPresented: Binding(get: { removal != nil }, set: { if !$0 { removal = nil } })) {
            Button("Cancel", role: .cancel) { removal = nil }
            Button("Remove Printer", role: .destructive) {
                if let printer = removal { Task { await model.remove(printer) } }
                removal = nil
            }
        } message: {
            Text("It will be removed from your Mac’s Print dialog. You can add it again anytime.")
        }
    }

    private var moreOptions: some View {
        Menu {
            Button("Printers & Scanners…") { openSettings() }
            Button("Open Diagnostics…") { model.diagnostics() }
            Divider()
            Button("Open Engine Terminal…") { model.openEngineTerminal() }
                .disabled(!model.connected)
                .help("A root shell in the Linux print engine, for driver installers that need to be run by hand")
            Button("Show Transfer Folder") { model.showTransferFolder() }
            Divider()
            Button("Quit lpbridge") { NSApp.terminate(nil) }
        } label: {
            Image(systemName: "ellipsis").font(.system(size: 16, weight: .medium))
                .foregroundStyle(.secondary).frame(width: 24, height: 24)
        }
        .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
        .help("Settings and diagnostics").accessibilityLabel("More options")
    }

    // Center the complete state in the window: equal insets top and bottom,
    // and the content fills the rest so its own centering holds at any size.
    // `splashOffset` nudges it (positive = down); 0 is dead center. Long
    // error messages can scroll without pushing their actions out of reach.
    private func centeredPage<Content: View>(@ViewBuilder content: () -> Content) -> some View {
        let pageContent = content()
        return GeometryReader { geometry in
            ScrollView {
                pageContent
                    .frame(maxWidth: .infinity)
                    .frame(minHeight: max(0, geometry.size.height - AppStyle.topInset * 2))
                    .offset(y: AppStyle.splashOffset)
            }
            .padding(.horizontal, AppStyle.inset)
            .padding(.vertical, AppStyle.topInset)
        }
        .overlay(alignment: .topTrailing) {
            moreOptions.padding(.trailing, AppStyle.inset).padding(.top, AppStyle.topInset)
        }
    }

    private func pageTitle(_ title: String) -> some View {
        Text(title).font(.system(size: 25, weight: .semibold)).tracking(-0.5)
    }

    private func heading(_ title: String, _ detail: String? = nil) -> some View {
        VStack(spacing: 10) {
            Text(title).font(.system(size: 24, weight: .semibold)).tracking(-0.4)
            if let detail = detail {
                Text(detail).font(.system(size: 13)).foregroundStyle(.secondary).lineSpacing(3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }.multilineTextAlignment(.center).frame(maxWidth: 420)
    }

    var welcome: some View {
        centeredPage {
            VStack(spacing: 24) {
                PrinterArtwork()
                heading("Make your printer feel at home", "Connect it with USB or join the same network.\nWe’ll take care of the driver.")
                Button("Set Up My Printer") { Task { await model.start() } }
                    .buttonStyle(.borderedProminent).controlSize(.large).keyboardShortcut(.defaultAction)
            }
        }
    }

    var preparing: some View {
        centeredPage {
            VStack(spacing: 24) {
                PrinterArtwork()
                heading(model.error == nil ? model.stage : "Couldn’t finish setup", model.error)
                if model.error == nil {
                    if let value = model.progress {
                        VStack(spacing: 10) {
                            ProgressView(value: value).progressViewStyle(.linear)
                            Text("\(Int(value * 100))% prepared").monospacedDigit()
                                .font(.system(size: 12)).foregroundStyle(.secondary)
                        }.frame(width: 260)
                    } else {
                        ProgressView().controlSize(.small).accessibilityLabel(model.stage)
                    }
                } else {
                    HStack(spacing: 12) {
                        Button("Open Diagnostics") { model.diagnostics() }
                        Button(model.legacyFound ? "Switch to New App" : "Try Again") {
                            if model.legacyFound { Task { await model.migrate() }; return }
                            model.error = nil
                            if model.connected { model.page = .discovery } else { Task { await model.start() } }
                        }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                    }.controlSize(.large)
                }
            }
        }
    }

    var discovery: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 16) {
                HStack(alignment: .center) {
                    VStack(alignment: .leading, spacing: 6) {
                        pageTitle("Find your printer")
                        Text("Connect with USB or join the same network.")
                            .font(.system(size: 13)).foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 12)
                    if model.scanning {
                        ProgressView().controlSize(.small).help("Looking for USB and network printers")
                    }
                    Button { Task { await model.discover(force: true) } } label: {
                        Image(systemName: "arrow.clockwise").frame(width: 20, height: 20)
                    }.buttonStyle(.borderless).foregroundStyle(.secondary)
                        .help("Search again").accessibilityLabel("Search again").disabled(model.busy || model.scanning)
                    moreOptions
                }
                HStack(spacing: 8) {
                    Image(systemName: "magnifyingglass").foregroundStyle(.tertiary)
                    TextField("Search printers", text: $model.query).textFieldStyle(.plain)
                    if !model.query.isEmpty {
                        Button { model.query = "" } label: {
                            Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
                        }.buttonStyle(.plain).accessibilityLabel("Clear search")
                    }
                }
                .font(.system(size: 13)).padding(.horizontal, 12).padding(.vertical, 10)
                .background(Color.primary.opacity(0.045), in: RoundedRectangle(cornerRadius: 8))

                if model.visibleDevices.isEmpty {
                    discoveryEmptyState.frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    ScrollView {
                        LazyVStack(spacing: 0) {
                            ForEach(model.visibleDevices) { device in
                                deviceRow(device)
                                if device.id != model.visibleDevices.last?.id {
                                    Divider().padding(.leading, 64)
                                }
                            }
                        }
                        .background(AppStyle.surface, in: RoundedRectangle(cornerRadius: 12))
                        .clipShape(RoundedRectangle(cornerRadius: 12))
                        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(AppStyle.border, lineWidth: 1))
                    }
                }
                if let selected = model.devices.first(where: { $0.id == model.selected }),
                   let note = selected.note, selected.available == false {
                    Text(note).font(.system(size: 12)).foregroundStyle(.secondary)
                }
                driverImport
                if let error = model.error {
                    Text(error).font(.system(size: 12)).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.horizontal, AppStyle.inset).padding(.top, AppStyle.topInset).padding(.bottom, 16)

            HStack {
                Button(model.printers.isEmpty ? "Set Up Later" : "Back to My Printers") {
                    model.page = .library; model.error = nil
                }.buttonStyle(.borderless).foregroundStyle(.secondary)
                Spacer()
                Button("Add Printer") {
                    guard let device = model.devices.first(where: { $0.id == model.selected }) else { return }
                    Task { await model.add(device, ppd: chosenDriver[device.id]) }
                }.buttonStyle(.borderedProminent).controlSize(.large).keyboardShortcut(.defaultAction)
                    .disabled(model.selected == nil || model.busy || !model.visibleDevices.contains(where: { $0.id == model.selected && $0.available != nil }))
            }
            .padding(.horizontal, AppStyle.inset).padding(.vertical, 16)
            .overlay(alignment: .top) { Rectangle().fill(AppStyle.border).frame(height: 1) }
        }
        .overlay {
            if driverDropTargeted {
                RoundedRectangle(cornerRadius: 14).strokeBorder(Color.accentColor, lineWidth: 2).padding(8).allowsHitTesting(false)
            }
        }
        .task { await model.discover() }
        .onDrop(of: [.fileURL], isTargeted: $driverDropTargeted) { providers in
            Task {
                var urls: [URL] = []
                for provider in providers {
                    if let data = try? await provider.loadItem(forTypeIdentifier: "public.file-url") as? Data,
                       let url = URL(dataRepresentation: data, relativeTo: nil) { urls.append(url) }
                }
                await model.addDriverFiles(urls)
            }
            return true
        }
    }

    private func deviceRow(_ device: Device) -> some View {
        let selected = model.selected == device.id
        return VStack(alignment: .leading, spacing: 0) {
            Button { model.selected = device.id } label: {
                HStack(spacing: 12) {
                    Image(systemName: "printer.fill").font(.system(size: 22))
                        .foregroundStyle(selected ? Color.accentColor : Color.secondary)
                        .frame(width: 32)
                    VStack(alignment: .leading, spacing: 4) {
                        Text(device.name).font(.system(size: 14, weight: .medium)).foregroundStyle(.primary)
                            .fixedSize(horizontal: false, vertical: true)
                        Text(device.available == nil ? "Looking up drivers…" : (device.available == false ? "No driver found yet" : "Driver: \(chosenLabel(device))"))
                            .font(.system(size: 12)).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: 12)
                    Text(device.transport == "usb-raw" ? "USB" : "Network")
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                    Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                        .font(.system(size: 16)).foregroundStyle(selected ? Color.accentColor : Color.secondary.opacity(0.35))
                }.padding(16).frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
            }.buttonStyle(.plain).accessibilityAddTraits(selected ? .isSelected : [])
            // The picker is a sibling of the selection button so both controls
            // receive clicks and keyboard focus independently.
            if selected, let candidates = device.candidates, candidates.count > 1 {
                Picker("Driver", selection: Binding(
                    get: { chosenDriver[device.id] ?? candidates[0].ppd_name },
                    set: { chosenDriver[device.id] = $0 })) {
                    ForEach(candidates) { candidate in Text(candidate.label).tag(candidate.ppd_name) }
                }.labelsHidden().pickerStyle(.menu).controlSize(.small)
                    .frame(maxWidth: 420, alignment: .leading)
                    .padding(.leading, 60).padding(.trailing, 16).padding(.bottom, 14)
                    .help("The recommended driver is listed first. You can choose a different one.")
            }
        }.background(selected ? Color.accentColor.opacity(0.08) : Color.clear)
    }

    private var discoveryEmptyState: some View {
        VStack(spacing: 10) {
            Image(systemName: model.query.isEmpty ? "antenna.radiowaves.left.and.right" : "magnifyingglass")
                .font(.system(size: 28, weight: .light)).foregroundStyle(.secondary).padding(.bottom, 6)
            Text(!model.query.isEmpty ? "No matching printers" : (model.scanning ? "Looking for printers…" : "No printers found yet"))
                .font(.system(size: 16, weight: .medium))
            Text(!model.query.isEmpty ? "Try a different name or clear your search." : (model.scanning ? "Checking USB and your local network." : "Check the cable or Wi-Fi connection, then search again."))
                .font(.system(size: 12)).foregroundStyle(.secondary).multilineTextAlignment(.center)
        }.padding(.vertical, 20)
    }

    private var driverImport: some View {
        HStack(spacing: 8) {
            if let notice = model.driverNotice {
                Text(notice).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            } else {
                Button { model.chooseDriverFile() } label: {
                    Label("Choose a driver file…", systemImage: "doc.badge.plus")
                }.buttonStyle(.link)
                Text("or drop a .ppd or .deb here").foregroundStyle(.tertiary)
            }
            Spacer(minLength: 0)
        }.font(.system(size: 12))
    }

    var library: some View {
        Group {
            if model.printers.isEmpty {
                centeredPage {
                    VStack(spacing: 16) {
                        emptyLibrary
                        libraryNotices
                    }
                }
            } else {
                VStack(alignment: .leading, spacing: 20) {
                    HStack(spacing: 14) {
                        pageTitle("Your printers")
                        Spacer()
                        addPrinterButton
                        moreOptions
                    }
                    libraryNotices
                    ScrollView {
                        VStack(spacing: 12) {
                            ForEach(model.printers) { printer in
                                printerCard(printer)
                            }
                        }
                    }
                }
                .padding(.horizontal, AppStyle.inset).padding(.top, AppStyle.topInset).padding(.bottom, 24)
            }
        }
    }

    @ViewBuilder private var libraryNotices: some View {
        if !model.connected {
            HStack {
                Label("Reconnecting to the print service…", systemImage: "wifi.exclamationmark")
                Spacer()
                Button("Reconnect") { Task { await model.start() } }
            }.font(.system(size: 12)).foregroundStyle(.secondary)
        }
        if let error = model.error {
            Text(error).font(.system(size: 12)).foregroundStyle(.red)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var emptyLibrary: some View {
        VStack(spacing: 20) {
            PrinterArtwork()
            VStack(spacing: 8) {
                Text("No printers added").font(.system(size: 20, weight: .semibold)).tracking(-0.3)
                Text("Add a printer to use it from any app.")
                    .font(.system(size: 13)).foregroundStyle(.secondary)
            }
            addPrinterButton
        }
    }

    private var addPrinterButton: some View {
        Button { model.selected = nil; model.error = nil; model.page = .discovery } label: {
            Label("Add Printer", systemImage: "plus")
        }.buttonStyle(.borderedProminent).controlSize(.large).disabled(!model.connected)
    }

    private func printerCard(_ printer: Printer) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 12) {
                Image(systemName: "printer.fill").font(.system(size: 24)).foregroundStyle(.secondary).frame(width: 32)
                VStack(alignment: .leading, spacing: 5) {
                    Text(printer.name).font(.system(size: 14, weight: .medium))
                    HStack(spacing: 6) {
                        Circle().fill(printer.offline || printer.fault?.isEmpty == false ? Color.orange : Color.green)
                            .frame(width: 5, height: 5)
                        Text(printer.offline ? "Offline · check the connection" : (printer.fault?.isEmpty == false ? "Needs attention" : (printer.queued > 0 ? "\(printer.queued) jobs in progress" : "Ready to print")))
                            .foregroundStyle(.secondary)
                    }.font(.system(size: 12))
                }
                Spacer()
                Menu {
                    Button("Remove Printer…", role: .destructive) { removal = printer }
                } label: {
                    Image(systemName: "ellipsis").frame(width: 24, height: 24)
                }.menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize().disabled(model.busy)
                    .accessibilityLabel("Options for \(printer.name)")
            }
            if !printer.jobs.isEmpty {
                Divider()
                ForEach(printer.jobs.prefix(3)) { job in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text(job.name ?? "Document").lineLimit(1)
                            Spacer()
                            Text(job.status).foregroundStyle(.secondary)
                        }.font(.system(size: 12))
                        if let error = job.error, !error.isEmpty {
                            Text(error).font(.system(size: 11)).foregroundStyle(.red).textSelection(.enabled)
                        }
                    }
                }
            }
        }.padding(16).background(AppStyle.surface, in: RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(AppStyle.border, lineWidth: 1))
    }

    var success: some View {
        centeredPage {
            VStack(spacing: 24) {
                PrinterArtwork(success: true)
                heading("Ready to print", "\(model.addedName) has been added.\nPress ⌘P in any app and choose your printer.")
                HStack(spacing: 12) {
                    Button("Add Another Printer") { model.selected = nil; model.page = .discovery }
                    Button("My Printers") { model.page = .library }
                        .buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                }.controlSize(.large)
            }
        }
    }

    func chosenLabel(_ device: Device) -> String {
        if let ppd = chosenDriver[device.id], let candidate = device.candidates?.first(where: { $0.ppd_name == ppd }) {
            return candidate.driver ?? ppd
        }
        return device.driver ?? "unknown"
    }
    func openSettings() {
        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.Print-Scan-Settings.extension")!)
    }
}

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let model = AppModel()
    var window: NSWindow!
    var statusItem: NSStatusItem!
    private var windowLayout = AppWindowLayout.splash
    private var windowSizes: [AppWindowLayout: NSSize] = [:]
    private var resizeGeneration = 0
    private var resizeInFlight = false
    func applicationDidFinishLaunching(_ notification: Notification) {
        windowLayout = model.windowLayout
        window = NSWindow(contentRect: NSRect(origin: .zero, size: windowLayout.size), styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView], backing: .buffered, defer: false)
        window.title = "lpbridge"
        window.titlebarAppearsTransparent = true
        window.titlebarSeparatorStyle = .none
        window.titleVisibility = .hidden
        window.delegate = self
        window.contentView = NSHostingView(rootView: RootView(model: model, onWindowLayoutChange: { [weak self] layout in
            self?.resizeWindow(for: layout)
        }))
        window.contentMinSize = windowLayout.minimumSize
        window.isReleasedWhenClosed = false
        window.center()
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = NSImage(systemSymbolName: "printer", accessibilityDescription: "lpbridge")
        let menu = NSMenu()
        let open = menu.addItem(withTitle: "Open lpbridge", action: #selector(show), keyEquivalent: "")
        open.target = self
        menu.addItem(.separator())
        let quit = menu.addItem(withTitle: "Quit lpbridge", action: #selector(quitApp), keyEquivalent: "q")
        quit.target = self
        statusItem.menu = menu
        let mainMenu = NSMenu()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About lpbridge", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit lpbridge", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let item = NSMenuItem(); item.submenu = appMenu; mainMenu.addItem(item)
        let editMenu = NSMenu(title: "Edit")
        for (title, action, key) in [("Cut", "cut:", "x"), ("Copy", "copy:", "c"), ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")] {
            editMenu.addItem(withTitle: title, action: Selector(action), keyEquivalent: key)
        }
        let edit = NSMenuItem(); edit.submenu = editMenu; mainMenu.addItem(edit)
        NSApp.mainMenu = mainMenu
        show()
    }
    private func resizeWindow(for layout: AppWindowLayout, restoringFromFullScreen: Bool = false) {
        guard layout != windowLayout || restoringFromFullScreen else { return }
        resizeGeneration += 1
        let generation = resizeGeneration
        if window.styleMask.contains(.fullScreen) {
            windowLayout = layout
            return
        }
        let previousFrame = window.frame
        // Keep the user's size for each layout while navigating between them.
        if !restoringFromFullScreen && !resizeInFlight {
            windowSizes[windowLayout] = window.contentRect(forFrameRect: previousFrame).size
        }
        windowLayout = layout
        let preferred = windowSizes[layout] ?? layout.size
        let size = NSSize(width: max(preferred.width, layout.minimumSize.width),
                          height: max(preferred.height, layout.minimumSize.height))
        var frame = window.frameRect(forContentRect: NSRect(origin: .zero, size: size))
        frame.origin = NSPoint(x: previousFrame.midX - frame.width / 2,
                               y: previousFrame.midY - frame.height / 2)
        if let screen = window.screen {
            let visible = screen.visibleFrame
            frame.size.width = min(frame.width, visible.width)
            frame.size.height = min(frame.height, visible.height)
            frame.origin.x = min(max(frame.minX, visible.minX), visible.maxX - frame.width)
            frame.origin.y = min(max(frame.minY, visible.minY), visible.maxY - frame.height)
        }
        // Raising the minimum before expansion can snap the frame to its final
        // size. Keep the smaller constraint until the animation completes.
        window.contentMinSize = AppWindowLayout.splash.minimumSize
        guard !restoringFromFullScreen && !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion else {
            resizeInFlight = false
            window.setFrame(frame, display: true)
            window.contentMinSize = layout.minimumSize
            return
        }
        resizeInFlight = true
        // setFrame(..., animate: true) blocks this SwiftUI onChange callback.
        // The animator proxy lets the new page render while the window resizes.
        NSAnimationContext.runAnimationGroup { context in
            context.duration = AppStyle.transitionDuration
            context.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
            window.animator().setFrame(frame, display: true)
        } completionHandler: { [weak self] in
            Task { @MainActor in
                guard let self = self, self.resizeGeneration == generation else { return }
                self.resizeInFlight = false
                self.window.contentMinSize = layout.minimumSize
            }
        }
    }

    func windowWillEnterFullScreen(_ notification: Notification) {
        windowSizes[windowLayout] = window.contentRect(forFrameRect: window.frame).size
    }
    func windowDidExitFullScreen(_ notification: Notification) {
        resizeWindow(for: model.windowLayout, restoringFromFullScreen: true)
    }

    @objc func show() { NSApp.setActivationPolicy(.regular); window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
    @objc func quitApp() { NSApp.terminate(nil) }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { show(); return true }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
}

MainActor.assumeIsolated {
    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    app.run()
}
