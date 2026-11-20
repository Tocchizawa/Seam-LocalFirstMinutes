// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "SeamMac",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "SeamCore", targets: ["SeamCore"]),
        .library(name: "SeamUI", targets: ["SeamUI"]),
        .library(name: "SeamWorkspace", type: .static, targets: ["SeamWorkspace"]),
        .library(name: "SeamWorkspacePreviews", type: .dynamic, targets: ["SeamWorkspacePreviews"]),
        .executable(name: "SeamApp", targets: ["SeamApp"]),
        .executable(name: "AppleSpeechTranscriber", targets: ["AppleSpeechTranscriber"]),
    ],
    targets: [
        .target(name: "SeamCore"),
        .target(name: "SeamUI", dependencies: ["SeamCore"]),
        .target(name: "SeamWorkspace", dependencies: ["SeamCore", "SeamUI"]),
        .target(name: "SeamWorkspacePreviews", dependencies: ["SeamCore", "SeamWorkspace"]),
        .executableTarget(name: "SeamApp", dependencies: ["SeamCore", "SeamUI", "SeamWorkspace"]),
        .executableTarget(name: "AppleSpeechTranscriber", dependencies: ["SeamCore"]),
        .testTarget(name: "SeamCoreTests", dependencies: ["SeamCore"]),
    ]
)
