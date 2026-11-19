// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "SeamMac",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "SeamCore", targets: ["SeamCore"]),
        .library(name: "SeamUI", targets: ["SeamUI"]),
        .executable(name: "SeamApp", targets: ["SeamApp"]),
    ],
    targets: [
        .target(name: "SeamCore"),
        .target(name: "SeamUI", dependencies: ["SeamCore"]),
        .executableTarget(name: "SeamApp", dependencies: ["SeamCore", "SeamUI"]),
        .testTarget(name: "SeamCoreTests", dependencies: ["SeamCore"]),
    ]
)
