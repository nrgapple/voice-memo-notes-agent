// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "VoiceMemoTranscriber",
    platforms: [
        .macOS("26.0"),
    ],
    dependencies: [
        .package(
            url: "https://github.com/FluidInference/FluidAudio.git",
            exact: "0.15.6"
        ),
    ],
    targets: [
        .executableTarget(
            name: "VoiceMemoTranscriber",
            dependencies: [
                .product(name: "FluidAudio", package: "FluidAudio"),
            ],
            path: "Sources/VoiceMemoTranscriber"
        ),
    ]
)
