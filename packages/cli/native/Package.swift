// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "LatticeShadowNative",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .library(
            name: "LatticeShadowIntents",
            targets: ["LatticeShadowIntents"]
        ),
        .executable(
            name: "latticeshadow-foundation-bridge",
            targets: ["LatticeShadowFoundationBridge"]
        )
    ],
    targets: [
        .target(
            name: "LatticeShadowIntents",
            path: "LatticeShadowIntents"
        ),
        .executableTarget(
            name: "LatticeShadowFoundationBridge",
            path: "LatticeShadowFoundationBridge"
        )
    ]
)
