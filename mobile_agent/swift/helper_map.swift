// map: draws a result's places on an Apple Maps snapshot (MapKit, off screen, no window and no Dock icon).
//   map <in.json> <out.png>
// in.json: {"places": [{"id", "name", "address"?, "lat"?, "lon"?}], "near"?: "San Francisco, CA",
//           "width": 720, "height": 360, "dark": false}
// Writes the PNG at 2x and prints one JSON line: {"width", "height", "places": [{"id", "lat", "lon", "x", "y"}],
// "missing": [ids]}; x and y are in points from the top left. A place without coordinates is found with Apple's
// geocoder (its address, then its name near "near"); one that can't be found is listed in "missing". Exit 1 with a
// line starting "Mobster" on stderr when there is nothing to draw.
import AppKit
import CoreImage
import Foundation
import MapKit

struct Place: Decodable { let id: String; let name: String; let address: String?; let lat: Double?; let lon: Double? }
struct Input: Decodable { let places: [Place]; let near: String?; let width: Double?; let height: Double?; let dark: Bool? }

func fail(_ message: String) -> Never {
  FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
  exit(1)
}

let args = CommandLine.arguments
guard args.count == 3 else { fail("Mobster's map helper takes <in.json> <out.png>") }
guard let data = FileManager.default.contents(atPath: args[1]),
      let input = try? JSONDecoder().decode(Input.self, from: data) else { fail("Mobster couldn't read the places") }
let width = min(1200, max(200, input.width ?? 720)), height = min(900, max(120, input.height ?? 360))
var found: [(Place, CLLocationCoordinate2D)] = []
var missing: [String] = []
let geocoder = CLGeocoder()

func valid(_ c: CLLocationCoordinate2D) -> Bool { CLLocationCoordinate2DIsValid(c) && !(c.latitude == 0 && c.longitude == 0) }

/// The place's coordinate: its own, else its address (with ``near`` when the address doesn't say it), else a search
/// for its name near ``near``.
func locate(_ place: Place, _ done: @escaping (CLLocationCoordinate2D?) -> Void) {
  if let lat = place.lat, let lon = place.lon { done(CLLocationCoordinate2D(latitude: lat, longitude: lon)); return }
  let near = input.near.map { ", " + $0 } ?? ""
  let search = {
    let request = MKLocalSearch.Request()
    request.naturalLanguageQuery = place.name + (place.address.map { ", " + $0 } ?? "") + near
    MKLocalSearch(request: request).start { response, _ in
      done(response?.mapItems.first.map { $0.placemark.coordinate })
    }
  }
  guard let address = place.address, !address.isEmpty else { search(); return }
  let city = (input.near ?? "").split(separator: ",").first.map { $0.trimmingCharacters(in: .whitespaces).lowercased() } ?? ""
  let text = city.isEmpty || address.lowercased().contains(city) ? address : address + near
  geocoder.geocodeAddressString(text) { marks, _ in
    if let c = marks?.first?.location?.coordinate, valid(c) { done(c) } else { search() }
  }
}

func next(_ index: Int) {
  if index == input.places.count { draw(); return }
  let place = input.places[index]
  locate(place) { coordinate in
    if let c = coordinate, valid(c) { found.append((place, c)) } else { missing.append(place.id) }
    next(index + 1)
  }
}

func draw() {
  guard !found.isEmpty else { fail("Mobster couldn't find any of these places on the map") }
  let options = MKMapSnapshotter.Options()
  var rect = MKMapRect.null
  for (_, c) in found { let p = MKMapPoint(c); rect = rect.union(MKMapRect(x: p.x, y: p.y, width: 1, height: 1)) }
  // Room around the pins (their heads stand above the point), and never closer than a few streets.
  let least = MKMapPointsPerMeterAtLatitude(found[0].1.latitude) * 1200
  let w = max(rect.size.width, least), h = max(rect.size.height, least * height / width)
  rect = MKMapRect(x: rect.midX - w / 2, y: rect.midY - h / 2, width: w, height: h)
  rect = rect.insetBy(dx: -w * 0.12, dy: -h * 0.2)
  options.mapRect = rect
  options.size = NSSize(width: width, height: height)
  options.appearance = NSAppearance(named: input.dark == true ? .darkAqua : .aqua)
  if #available(macOS 14.0, *) {  // options.preferredConfiguration is macOS 14 and later
    let configuration = MKStandardMapConfiguration(elevationStyle: .flat, emphasisStyle: .muted)
    configuration.pointOfInterestFilter = .excludingAll
    options.preferredConfiguration = configuration
  } else {
    options.pointOfInterestFilter = .excludingAll
  }
  MKMapSnapshotter(options: options).start { snapshot, _ in
    guard let snapshot = snapshot, let image = snapshot.image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
      fail("Mobster couldn't draw the map. Check this Mac's internet connection")
    }
    // Quieter than Apple's own colours, so the pins and the page's tokens lead (dark Maps is saturated navy).
    var picture = image
    let filter = CIFilter(name: "CIColorControls")
    filter?.setValue(CIImage(cgImage: image), forKey: kCIInputImageKey)
    filter?.setValue(input.dark == true ? 0.22 : 0.7, forKey: kCIInputSaturationKey)
    if let output = filter?.outputImage, let quiet = CIContext().createCGImage(output, from: output.extent) { picture = quiet }
    let rep = NSBitmapImageRep(cgImage: picture)
    guard let png = rep.representation(using: .png, properties: [:]) else { fail("Mobster couldn't save the map") }
    do { try png.write(to: URL(fileURLWithPath: args[2])) } catch { fail("Mobster couldn't save the map") }
    let places: [[String: Any]] = found.map { place, c in
      let point = snapshot.point(for: c)
      // AppKit's snapshot points start at the bottom left; the page's start at the top left.
      return ["id": place.id, "lat": c.latitude, "lon": c.longitude, "x": Double(point.x), "y": height - Double(point.y)]
    }
    let out: [String: Any] = ["width": width, "height": height, "places": places, "missing": missing]
    let json = try! JSONSerialization.data(withJSONObject: out)
    FileHandle.standardOutput.write(json)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
    exit(0)
  }
}

next(0)
RunLoop.main.run(until: Date(timeIntervalSinceNow: 40))
fail("Mobster took too long to draw the map")
