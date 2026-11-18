// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "SeamMac",
    platforms: [.macOS(.v13)],
    products: [.library(name: "SeamCore", targets: ["SeamCore"])],
    targets: [.target(name: "SeamCore")]
)
