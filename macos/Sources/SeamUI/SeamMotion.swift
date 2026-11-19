import SwiftUI

public enum SeamMotion {
    // 共通の短い反応・更新アニメーション。秒数を変えると各部品に反映される。
    public static let feedback = Animation.easeOut(duration: 0.13)
    public static let update = Animation.easeOut(duration: 0.19)

    public static var reposition: Animation {
        if #available(macOS 14.0, *) { return .smooth(duration: 0.25) }
        return .easeOut(duration: 0.25)
    }
}
