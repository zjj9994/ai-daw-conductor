-- =====================================================================
-- Logic Pro 一次性环境准备脚本
-- 在 Logic Pro 中启用脚本控制与外部 MIDI 输入，提升自动化可靠性。
-- 用法：在 macOS 上运行  osascript scripts/logic_setup.scpt
-- =====================================================================

on run
	tell application "Logic Pro"
		activate
	end tell
	
	-- 提示用户在 Logic Pro 偏好设置里完成以下手动步骤
	display dialog "请在 Logic Pro 中确认以下设置已开启：

1. Logic Pro > 设置 > 通用 > 高级工具 → 勾选「启用高级工具」
   （开启后才能用 AppleScript 与完整键命令）

2. Logic Pro > 设置 > MIDI > 输入 → 勾选「AI-DAW-Conductor」虚拟端口
   （首次运行后端后会自动创建该虚拟 MIDI 端口）

3. Logic Pro > 设置 > 自动化 → 确认允许外部控制

4. 系统设置 > 隐私与安全 > 辅助功能 → 允许「终端 / iTerm」控制 Logic Pro
   （AppleScript 模拟键命令需要辅助功能权限）

完成后点「好」继续。" with title "AI-DAW-Conductor 环境准备" buttons {"好"} default button 1
	
	-- 验证 Logic Pro 正在运行
	tell application "System Events"
		if not (exists process "Logic Pro") then
			display dialog "Logic Pro 未运行，请先打开 Logic Pro。" with title "提示" buttons {"好"} default button 1
			return
		end if
	end tell
	
	display dialog "环境检查完成。现在可以在网页端 http://127.0.0.1:8787 开始让 AI 指挥作曲了。" with title "就绪" buttons {"好"} default button 1
end run
