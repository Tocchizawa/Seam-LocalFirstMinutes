import SwiftUI

public enum SeamMotion {
    public static let feedback = Animation.easeOut(duration: 0.13)
    public static let update = Animation.easeOut(duration: 0.19)

    public static var reposition: Animation {
        if #available(macOS 14.0, *) { return .smooth(duration: 0.25) }
        return .easeOut(duration: 0.25)
    }
}
