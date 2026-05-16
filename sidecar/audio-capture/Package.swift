// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "audio-capture",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "audio-capture",
            path: "Sources",
            linkerSettings: [
                .linkedFramework("ScreenCaptureKit"),
                .linkedFramework("CoreMedia"),
                .linkedFramework("AudioToolbox"),
                .linkedFramework("AVFoundation"),
            ]
        ),
    ]
)
