// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "JarvisShared",
    platforms: [.iOS(.v16), .macOS(.v14)],
    products: [
        .library(name: "JarvisShared", targets: ["JarvisShared"]),
    ],
    targets: [
        .target(
            name: "JarvisShared",
            path: "Sources/JarvisShared"
        ),
    ]
)
