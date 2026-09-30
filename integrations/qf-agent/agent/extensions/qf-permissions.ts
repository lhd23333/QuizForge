/**
 * QuizForge 权限门（pi extension）
 *
 * 只拦截 `mcp__quizforge__*` 的调用，用 MCP annotations 判断风险：
 * 写操作（destructiveHint / 非 readOnlyHint）在执行前弹确认；只读调用直接放行。
 * 不影响 pi 自身的 bash / edit / write 等工具，也不动其它 MCP server。
 *
 * 权限档（环境变量 QF_PERMISSION）：
 *   standard（默认）  写操作逐次确认，可对单个工具选择「本会话内全部允许」
 *   full             完全放开，不确认（等价 QuizForge 的 full）
 *   read-only        只读，任何非只读调用直接拒绝
 *
 * 非交互模式（pi -p / JSON / RPC，没有 UI）默认拒绝写操作，避免自动化里静默落盘；
 * 确实需要时设 QF_PERMISSION_NONINTERACTIVE=1 放行。
 *
 * 安装：复制到 ~/.pi/agent/extensions/ 后 /reload（或新开会话）。
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const PREFIX = "mcp__quizforge__";

export default function (pi: ExtensionAPI) {
	const level = String(process.env.QF_PERMISSION ?? "standard").trim().toLowerCase();
	const autoApprove = level === "full" || level === "danger";
	const readOnly = level === "read-only" || level === "readonly";
	const allowNonInteractive = process.env.QF_PERMISSION_NONINTERACTIVE === "1";

	// 「本会话内全部允许」按工具名记录，不放大到所有写操作。
	const allowedThisSession = new Set<string>();

	pi.on("tool_call", async (event, ctx) => {
		const full = event.toolName;
		if (!full.startsWith(PREFIX)) return undefined;

		const tool = full.slice(PREFIX.length);
		const hints = pi.getAllTools().find((t) => t.name === full)?.annotations;
		const isWrite = hints?.destructiveHint === true || hints?.readOnlyHint !== true;

		if (!isWrite) return undefined;

		if (readOnly) {
			if (ctx.hasUI) ctx.ui.notify(`只读模式：已拒绝 ${tool}`, "warning");
			return { block: true, reason: `只读模式（QF_PERMISSION=read-only）拒绝了写操作 ${tool}` };
		}

		if (autoApprove || allowedThisSession.has(tool)) return undefined;

		const detail = JSON.stringify(event.input ?? {}, null, 2).slice(0, 500);

		if (!ctx.hasUI) {
			if (allowNonInteractive) return undefined;
			return {
				block: true,
				reason:
					`非交互模式下 ${tool} 需要确认但无法弹出提示；` +
					`如确实要放行，设置 QF_PERMISSION_NONINTERACTIVE=1 或 QF_PERMISSION=full`,
			};
		}

		const choice = await ctx.ui.select(
			`QuizForge 写操作：${tool}\n\n${detail}\n\n允许这次调用吗？`,
			["允许", "本会话内全部允许此工具", "拒绝"],
		);

		if (choice === "允许") return undefined;
		if (choice === "本会话内全部允许此工具") {
			allowedThisSession.add(tool);
			ctx.ui.notify(`本会话将不再确认 ${tool}`, "info");
			return undefined;
		}
		return { block: true, reason: `用户拒绝了 QuizForge 写操作 ${tool}` };
	});
}
