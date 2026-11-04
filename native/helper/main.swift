//
//  plannerkit — the Planner's native bridge to macOS (EventKit + PDFKit).
//
//  One small CLI, JSON in / JSON out, so the Python backend never has to speak
//  AppleScript. EventKit gives us what JXA never could:
//    * recurring series created / deleted correctly (span: this|future|all)
//    * real occurrences of recurring events when reading a range
//    * a live change feed (`watch`) driven by EKEventStoreChanged
//
//  Commands (all print one JSON document on stdout, errors as {"error": ...}):
//    status                         authorization status
//    request                        ask for full calendar access (prompts once)
//    calendars                      list event calendars
//    ensure-calendar <name>         create the calendar if missing
//    events --from T --to T [--calendars '["A","B"]']
//    create   < {calendar,title,start,end,notes?,location?,all_day?,recurrence?}
//    update   < {id, occurrence?, span?, title?, start?, end?, notes?, location?, recurrence?}
//    delete   < {id, occurrence?, span?}            span: this | future | all
//    watch                          one JSON line per (debounced) store change
//    pdf-text <path> [maxChars]     plain text of a PDF
//
//  Times are local wall-clock "YYYY-MM-DDTHH:MM[:SS]".
//
import EventKit
import Foundation
import PDFKit

setvbuf(stdout, nil, _IOLBF, 0)

// MARK: - JSON helpers

func emit(_ obj: Any) {
    if let data = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys]),
       let text = String(data: data, encoding: .utf8) {
        print(text)
    } else {
        print("{\"error\":\"could not encode output\"}")
    }
    fflush(stdout)
}

func fail(_ message: String, code: Int32 = 1) -> Never {
    emit(["error": message])
    exit(code)
}

func readStdinJSON() -> [String: Any] {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    guard !data.isEmpty,
          let obj = try? JSONSerialization.jsonObject(with: data),
          let dict = obj as? [String: Any] else {
        fail("expected a JSON object on stdin")
    }
    return dict
}

// MARK: - Dates (local wall clock)

let localFormatters: [DateFormatter] = {
    ["yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd'T'HH:mm", "yyyy-MM-dd HH:mm:ss", "yyyy-MM-dd HH:mm", "yyyy-MM-dd"].map {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone.current
        f.dateFormat = $0
        return f
    }
}()

func parseDate(_ value: Any?) -> Date? {
    guard let s = value as? String, !s.isEmpty else { return nil }
    for f in localFormatters {
        if let d = f.date(from: s) { return d }
    }
    return nil
}

func fmt(_ d: Date?) -> Any {
    guard let d = d else { return NSNull() }
    return localFormatters[0].string(from: d)
}

// MARK: - Store / authorization

let store = EKEventStore()

func authString() -> String {
    let status = EKEventStore.authorizationStatus(for: .event)
    switch status {
    case .fullAccess: return "fullAccess"
    case .writeOnly: return "writeOnly"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .notDetermined: return "notDetermined"
    @unknown default: return "unknown"
    }
}

func requireAccess(forReading: Bool = false) {
    let s = authString()
    if s == "fullAccess" { return }
    if !forReading && s == "writeOnly" { return }
    fail("calendar access is '\(s)'. Grant 'Full Access' in System Settings › Privacy & Security › Calendars, or run `plannerkit request`.", code: 3)
}

func norm(_ s: String) -> String {
    return s.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
}

func findCalendar(_ name: String) -> EKCalendar? {
    let cals = store.calendars(for: .event)
    if let exact = cals.first(where: { $0.title == name }) { return exact }
    return cals.first(where: { norm($0.title) == norm(name) })
}

func preferredSource() -> EKSource? {
    // Prefer iCloud (CalDAV), then the default calendar's source, then local.
    if let icloud = store.sources.first(where: { $0.sourceType == .calDAV && $0.title.lowercased().contains("icloud") }) {
        return icloud
    }
    if let def = store.defaultCalendarForNewEvents?.source { return def }
    return store.sources.first(where: { $0.sourceType == .local })
}

// MARK: - Event serialization

func weekdayIndex(_ wd: EKWeekday) -> Int {
    // EKWeekday: sunday=1 ... saturday=7  ->  Mon=0 ... Sun=6
    return (wd.rawValue + 5) % 7
}

func ekWeekday(_ idx: Int) -> EKWeekday {
    return EKWeekday(rawValue: ((idx + 1) % 7) + 1) ?? .monday
}

func describeRule(_ rule: EKRecurrenceRule) -> [String: Any] {
    var out: [String: Any] = ["interval": rule.interval]
    switch rule.frequency {
    case .daily: out["freq"] = "daily"
    case .weekly: out["freq"] = "weekly"
    case .monthly: out["freq"] = "monthly"
    case .yearly: out["freq"] = "yearly"
    @unknown default: out["freq"] = "other"
    }
    if let days = rule.daysOfTheWeek {
        out["days"] = days.map { weekdayIndex($0.dayOfTheWeek) }.sorted()
    }
    if let end = rule.recurrenceEnd?.endDate { out["until"] = fmt(end) }
    return out
}

func serialize(_ e: EKEvent) -> [String: Any] {
    let attendeesList = (e.attendees ?? []).compactMap { p -> String? in
        if let name = p.name, !name.isEmpty { return name }
        let u = p.url.absoluteString.replacingOccurrences(of: "mailto:", with: "")
        return u.isEmpty ? nil : u
    }
    var d: [String: Any] = [
        "id": e.eventIdentifier ?? "",
        "item_id": e.calendarItemIdentifier,
        "ext_id": e.calendarItemExternalIdentifier ?? "",
        "calendar": e.calendar?.title ?? "",
        "source": e.calendar?.source.title ?? "",
        "writable": e.calendar?.allowsContentModifications ?? true,
        "title": e.title ?? "",
        "start": fmt(e.startDate),
        "end": fmt(e.endDate),
        "all_day": e.isAllDay,
        "location": e.location ?? "",
        "url": e.url?.absoluteString ?? "",
        "notes": String((e.notes ?? "").prefix(4000)),
        "organizer": e.organizer?.name ?? "",
        "attendees": attendeesList,
        "recurring": e.hasRecurrenceRules,
        "detached": e.isDetached,
        "occurrence": fmt(e.occurrenceDate),
        "modified": fmt(e.lastModifiedDate),
    ]
    if let rule = e.recurrenceRules?.first { d["rule"] = describeRule(rule) }
    return d
}

func buildRule(_ spec: [String: Any]) -> EKRecurrenceRule? {
    let freqName = (spec["freq"] as? String ?? "weekly").lowercased()
    let interval = max(1, spec["interval"] as? Int ?? 1)
    var end: EKRecurrenceEnd? = nil
    if let until = parseDate(spec["until"]) {
        end = EKRecurrenceEnd(end: until)
    } else if let count = spec["count"] as? Int, count > 0 {
        end = EKRecurrenceEnd(occurrenceCount: count)
    }
    switch freqName {
    case "daily":
        return EKRecurrenceRule(recurrenceWith: .daily, interval: interval, end: end)
    case "weekly":
        let days = (spec["days"] as? [Int] ?? []).filter { (0...6).contains($0) }
        let dow: [EKRecurrenceDayOfWeek]? = days.isEmpty ? nil : days.map { EKRecurrenceDayOfWeek(ekWeekday($0)) }
        return EKRecurrenceRule(recurrenceWith: .weekly, interval: interval, daysOfTheWeek: dow,
                                daysOfTheMonth: nil, monthsOfTheYear: nil, weeksOfTheYear: nil,
                                daysOfTheYear: nil, setPositions: nil, end: end)
    case "monthly":
        return EKRecurrenceRule(recurrenceWith: .monthly, interval: interval, end: end)
    default:
        return nil
    }
}

/// Resolve the event a mutation refers to. With `occurrence`, the specific
/// occurrence of a recurring series (matched by original or current start).
func resolveEvent(_ req: [String: Any]) -> EKEvent {
    let id = (req["id"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    let itemId = (req["item_id"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    let extId = (req["ext_id"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    if id.isEmpty && itemId.isEmpty && extId.isEmpty {
        fail("missing 'id'")
    }

    let lastPart: String = {
        if id.contains(":") {
            let parts = id.components(separatedBy: ":")
            if parts.count >= 2, let lp = parts.last, !lp.isEmpty { return lp }
        }
        return ""
    }()

    var target: EKEvent? = nil
    if !id.isEmpty {
        target = store.event(withIdentifier: id)
            ?? (store.calendarItem(withIdentifier: id) as? EKEvent)
            ?? store.calendarItems(withExternalIdentifier: id).compactMap { $0 as? EKEvent }.first
    }
    if target == nil && !itemId.isEmpty {
        target = (store.calendarItem(withIdentifier: itemId) as? EKEvent)
            ?? store.event(withIdentifier: itemId)
            ?? store.calendarItems(withExternalIdentifier: itemId).compactMap { $0 as? EKEvent }.first
    }
    if target == nil && !extId.isEmpty {
        target = store.calendarItems(withExternalIdentifier: extId).compactMap { $0 as? EKEvent }.first
            ?? store.event(withIdentifier: extId)
            ?? (store.calendarItem(withIdentifier: extId) as? EKEvent)
    }
    if target == nil && !lastPart.isEmpty {
        target = store.event(withIdentifier: lastPart)
            ?? (store.calendarItem(withIdentifier: lastPart) as? EKEvent)
            ?? store.calendarItems(withExternalIdentifier: lastPart).compactMap { $0 as? EKEvent }.first
    }

    let occDate = parseDate(req["occurrence"]) ?? parseDate(req["start"])
    if let occ = occDate {
        let pred = store.predicateForEvents(withStart: occ.addingTimeInterval(-48 * 3600),
                                            end: occ.addingTimeInterval(48 * 3600),
                                            calendars: target?.calendar.map { [$0] })
        let candidates = store.events(matching: pred).filter { ev in
            let eid = ev.eventIdentifier ?? ""
            let iid = ev.calendarItemIdentifier
            let xid = ev.calendarItemExternalIdentifier ?? ""
            if !id.isEmpty && (eid == id || iid == id || xid == id) { return true }
            if !itemId.isEmpty && (iid == itemId || eid == itemId) { return true }
            if !extId.isEmpty && (xid == extId || eid == extId) { return true }
            if !lastPart.isEmpty && (xid == lastPart || iid == lastPart || eid.hasSuffix(lastPart)) { return true }
            if let m = target {
                if eid == m.eventIdentifier || iid == m.calendarItemIdentifier { return true }
                if let mx = m.calendarItemExternalIdentifier, !mx.isEmpty, xid == mx { return true }
            }
            return false
        }
        if let hit = candidates.first(where: {
            abs(($0.occurrenceDate ?? $0.startDate).timeIntervalSince(occ)) < 120
                || abs($0.startDate.timeIntervalSince(occ)) < 120
        }) {
            return hit
        }
        if target == nil, let firstHit = candidates.first {
            return firstHit
        }
    }

    guard let master = target else { fail("event not found: \(id.isEmpty ? (itemId.isEmpty ? extId : itemId) : id)", code: 4) }
    return master
}

func span(_ req: [String: Any]) -> EKSpan {
    let s = (req["span"] as? String ?? "this").lowercased()
    return (s == "future" || s == "all") ? .futureEvents : .thisEvent
}

// MARK: - Commands

func cmdEvents(_ args: [String]) {
    requireAccess(forReading: true)
    var from: Date? = nil, to: Date? = nil, names: [String]? = nil, refresh = false
    var i = 0
    while i < args.count {
        let a = args[i]
        let v = i + 1 < args.count ? args[i + 1] : ""
        switch a {
        case "--from": from = parseDate(v); i += 2
        case "--to": to = parseDate(v); i += 2
        case "--calendars":
            if let data = v.data(using: .utf8), let arr = try? JSONSerialization.jsonObject(with: data) as? [String] {
                names = arr
            }
            i += 2
        case "--refresh":
            refresh = true; i += 1
        default: i += 1
        }
    }
    if refresh {
        store.refreshSourcesIfNecessary()
    }
    guard let start = from, let end = to else { fail("--from and --to are required") }
    var cals: [EKCalendar]? = nil
    var missing: [String] = []
    if let names = names {
        var found: [EKCalendar] = []
        for n in names {
            if let c = findCalendar(n) { found.append(c) } else { missing.append(n) }
        }
        if found.isEmpty {
            emit(["events": [], "missing": missing])
            return
        }
        cals = found
    }
    let pred = store.predicateForEvents(withStart: start, end: end, calendars: cals)
    let events = store.events(matching: pred).sorted { $0.startDate < $1.startDate }
    emit(["events": events.map(serialize), "missing": missing])
}

func cmdCreate() {
    requireAccess()
    let req = readStdinJSON()
    guard let calName = req["calendar"] as? String, let cal = findCalendar(calName) else {
        fail("calendar not found: \(req["calendar"] ?? "")", code: 4)
    }
    guard let start = parseDate(req["start"]), let end = parseDate(req["end"]) else {
        fail("start/end required")
    }
    let e = EKEvent(eventStore: store)
    e.calendar = cal
    e.title = req["title"] as? String ?? "(untitled)"
    e.startDate = start
    e.endDate = end
    e.isAllDay = req["all_day"] as? Bool ?? false
    if let n = req["notes"] as? String { e.notes = n }
    if let l = req["location"] as? String { e.location = l }
    if let u = req["url"] as? String {
        let trimmed = u.trimmingCharacters(in: .whitespacesAndNewlines)
        e.url = trimmed.isEmpty ? nil : URL(string: trimmed)
    }
    if let rec = req["recurrence"] as? [String: Any], let rule = buildRule(rec) {
        e.addRecurrenceRule(rule)
    }
    do {
        try store.save(e, span: .futureEvents, commit: true)
    } catch {
        fail("save failed: \(error.localizedDescription)")
    }
    emit(["ok": true, "event": serialize(e)])
}

func cmdUpdate() {
    requireAccess()
    let req = readStdinJSON()
    let e = resolveEvent(req)
    if let cal = e.calendar, !cal.allowsContentModifications {
        fail("calendar '\(cal.title)' is read-only and cannot be modified", code: 4)
    }
    if let t = req["title"] as? String { e.title = t }
    if let s = parseDate(req["start"]) { e.startDate = s }
    if let en = parseDate(req["end"]) { e.endDate = en }
    if let n = req["notes"] as? String { e.notes = n }
    if let l = req["location"] as? String { e.location = l }
    if let u = req["url"] as? String {
        let trimmed = u.trimmingCharacters(in: .whitespacesAndNewlines)
        e.url = trimmed.isEmpty ? nil : URL(string: trimmed)
    }
    if let calName = req["calendar"] as? String {
        guard let cal = findCalendar(calName) else { fail("calendar not found: \(calName)", code: 4) }
        e.calendar = cal
    }
    if req.keys.contains("recurrence") {
        for r in e.recurrenceRules ?? [] { e.removeRecurrenceRule(r) }
        if let rec = req["recurrence"] as? [String: Any], let rule = buildRule(rec) {
            e.addRecurrenceRule(rule)
        }
    }
    do {
        try store.save(e, span: span(req), commit: true)
    } catch {
        fail("update failed: \(error.localizedDescription)")
    }
    emit(["ok": true, "event": serialize(e)])
}

func cmdDelete() {
    requireAccess()
    let req = readStdinJSON()
    var e = resolveEvent(req)
    if let cal = e.calendar, !cal.allowsContentModifications {
        fail("calendar '\(cal.title)' is read-only and cannot be modified", code: 4)
    }
    if (req["span"] as? String ?? "").lowercased() == "all" {
        // Looking up by eventIdentifier returns the series' master/first occurrence,
        // so deleting it with .futureEvents removes the entire recurring series.
        if let eid = e.eventIdentifier, let master = store.event(withIdentifier: eid) {
            e = master
        }
    }
    do {
        try store.remove(e, span: span(req), commit: true)
    } catch {
        fail("delete failed: \(error.localizedDescription)")
    }
    emit(["ok": true])
}

func cmdEnsureCalendar(_ name: String) {
    requireAccess()
    if let c = findCalendar(name) {
        emit(["ok": true, "created": false, "calendar": c.title])
        return
    }
    guard let source = preferredSource() else { fail("no calendar source available") }
    let cal = EKCalendar(for: .event, eventStore: store)
    cal.title = name
    cal.source = source
    do {
        try store.saveCalendar(cal, commit: true)
    } catch {
        // iCloud sometimes refuses; fall back to the local source.
        if let local = store.sources.first(where: { $0.sourceType == .local }), local != source {
            cal.source = local
            do { try store.saveCalendar(cal, commit: true) } catch {
                fail("could not create calendar: \(error.localizedDescription)")
            }
        } else {
            fail("could not create calendar: \(error.localizedDescription)")
        }
    }
    emit(["ok": true, "created": true, "calendar": cal.title, "source": cal.source.title])
}

func cmdCalendars() {
    requireAccess()
    let cals = store.calendars(for: .event).map { c -> [String: Any] in
        ["name": c.title, "title": c.title, "id": c.calendarIdentifier, "source": c.source.title,
         "writable": c.allowsContentModifications]
    }
    emit(["calendars": cals])
}

func cmdRequest() {
    let sem = DispatchSemaphore(value: 0)
    var granted = false
    var errText: String? = nil
    store.requestFullAccessToEvents { ok, err in
        granted = ok
        errText = err?.localizedDescription
        sem.signal()
    }
    _ = sem.wait(timeout: .now() + 120)
    var out: [String: Any] = ["granted": granted, "auth": authString()]
    if let e = errText { out["detail"] = e }
    emit(out)
}

final class Watcher: NSObject {
    var pending: DispatchWorkItem?
    @objc func changed(_ note: Notification) {
        pending?.cancel()
        let work = DispatchWorkItem {
            emit(["type": "changed", "ts": fmt(Date())])
        }
        pending = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8, execute: work)
    }
}

func cmdWatch() {
    requireAccess(forReading: true)
    let w = Watcher()
    NotificationCenter.default.addObserver(w, selector: #selector(Watcher.changed(_:)),
                                           name: .EKEventStoreChanged, object: store)
    emit(["type": "ready", "ts": fmt(Date())])
    // Nudge remote (iCloud/Google) sources so server-side edits arrive quickly.
    Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { _ in
        store.refreshSourcesIfNecessary()
    }
    // Exit when the parent goes away (stdin closed).
    DispatchQueue.global().async {
        _ = FileHandle.standardInput.readDataToEndOfFile()
        exit(0)
    }
    RunLoop.main.run()
}

func cmdPdfText(_ args: [String]) {
    guard let path = args.first else { fail("pdf-text <path> [maxChars]") }
    let maxChars = args.count > 1 ? (Int(args[1]) ?? 60000) : 60000
    guard let doc = PDFDocument(url: URL(fileURLWithPath: path)) else {
        fail("cannot open PDF: \(path)", code: 4)
    }
    if doc.isLocked { fail("PDF is encrypted", code: 4) }
    var text = ""
    var truncated = false
    for i in 0..<doc.pageCount {
        guard let page = doc.page(at: i) else { continue }
        let pageText = page.string ?? ""
        text += "\n--- page \(i + 1) ---\n" + pageText
        if text.count > maxChars {
            text = String(text.prefix(maxChars))
            truncated = true
            break
        }
    }
    emit(["text": text, "pages": doc.pageCount, "truncated": truncated])
}

// MARK: - Main

let argv = Array(CommandLine.arguments.dropFirst())
guard let cmd = argv.first else {
    fail("usage: plannerkit status|request|calendars|ensure-calendar|events|create|update|delete|watch|pdf-text")
}
let rest = Array(argv.dropFirst())

switch cmd {
case "status": emit(["auth": authString()])
case "request": cmdRequest()
case "calendars": cmdCalendars()
case "refresh":
    requireAccess(forReading: true)
    store.refreshSourcesIfNecessary()
    emit(["ok": true, "calendars": store.calendars(for: .event).count])
case "ensure-calendar":
    guard let name = rest.first else { fail("ensure-calendar <name>") }
    cmdEnsureCalendar(name)
case "events": cmdEvents(rest)
case "create": cmdCreate()
case "update": cmdUpdate()
case "delete": cmdDelete()
case "watch": cmdWatch()
case "pdf-text": cmdPdfText(rest)
default: fail("unknown command: \(cmd)")
}
