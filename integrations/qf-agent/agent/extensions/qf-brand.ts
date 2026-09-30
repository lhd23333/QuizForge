/**
 * QuizForge 品牌扩展
 *
 * 把终端里肉眼可见的 pi 痕迹换成 QuizForge：
 *   - 顶部 header 换成 QF 标志 + 题库/权限信息（替换 pi 自带的 logo 与提示）
 *   - 终端窗口标题设为 QuizForge（工作时带 braille spinner）
 *   - 底部状态栏追加一行 QuizForge 状态（题库 · 权限 · 模型）
 *
 * 只做呈现，不碰任何工具与权限逻辑（那是 qf-permissions.ts 的事）。
 */
import path from "node:path";
import type { ExtensionAPI, Theme } from "@earendil-works/pi-coding-agent";
import { truncateToWidth } from "@earendil-works/pi-tui";

const BRAND = "QuizForge";
const TAGLINE = "本地题库助手";

/** QF 标志（等宽块字符，全单宽，窄终端安全）。 */
const LOGO = [
	" ██████╗ ███████╗",
	"██╔═══██╗██╔════╝",
	"██║   ██║█████╗",
	"██║▄▄ ██║██╔══╝",
	"╚██████╔╝██║",
	" ╚══▀▀═╝ ╚═╝",
];

const SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];

function bankLabel(): string {
	const bank = (process.env.QUIZFORGE_BANK || "").trim();
	if (!bank) return "未指定题库";
	return path.basename(bank.replace(/[\\/]+$/, "")) || "题库";
}

function workdirLabel(): string {
	const workdir = (process.env.QF_WORKDIR || "").trim();
	return workdir || "根目录";
}

function permissionLabel(): string {
	const level = (process.env.QF_PERMISSION || "standard").trim().toLowerCase();
	if (level === "full" || level === "danger") return "完全放开";
	if (level === "read-only" || level === "readonly") return "只读";
	return "标准";
}

function renderHeader(theme: Theme, width: number, modelId?: string): string[] {
	const lines: string[] = [];
	for (const line of LOGO) {
		lines.push(truncateToWidth(theme.fg("accent", line), width));
	}
	lines.push(
		truncateToWidth(
			` ${theme.fg("accent", BRAND)}${theme.fg("dim", ` · ${TAGLINE}`)}`,
			width,
		),
	);
	const meta = [
		`题库 ${bankLabel()}`,
		`权限 ${permissionLabel()}`,
		modelId ? `模型 ${modelId}` : "",
	]
		.filter(Boolean)
		.join(" · ");
	lines.push(truncateToWidth(theme.fg("muted", ` ${meta}`), width));
	lines.push("");
	return lines;
}

export default function (pi: ExtensionAPI) {
	let timer: ReturnType<typeof setInterval> | null = null;
	let frame = 0;

	const baseTitle = () => `${BRAND} — ${bankLabel()} — ${workdirLabel()}`;

	function stopSpinner(ctx: { ui: { setTitle(title: string): void } }) {
		if (timer) {
			clearInterval(timer);
			timer = null;
		}
		frame = 0;
		ctx.ui.setTitle(baseTitle());
	}

	function startSpinner(ctx: { ui: { setTitle(title: string): void } }) {
		stopSpinner(ctx);
		timer = setInterval(() => {
			const glyph = SPINNER[frame % SPINNER.length];
			frame++;
			ctx.ui.setTitle(`${glyph} ${baseTitle()}`);
		}, 90);
	}

	pi.on("session_start", async (_event, ctx) => {
		ctx.ui.setTitle(baseTitle());
		if (ctx.mode === "tui") {
			ctx.ui.setHeader((_tui, theme) => ({
				render(width: number): string[] {
					return renderHeader(theme, width, ctx.model?.id);
				},
				invalidate() {},
			}));
		}
		const theme = ctx.ui.theme;
		ctx.ui.setStatus(
			"quizforge",
			theme.fg("dim", ` ${BRAND} 题库 ${bankLabel()} · 权限 ${permissionLabel()}`),
		);
	});

	pi.on("agent_start", async (_event, ctx) => {
		if (ctx.mode === "tui") startSpinner(ctx);
	});

	pi.on("agent_settled", async (_event, ctx) => {
		if (ctx.mode === "tui") stopSpinner(ctx);
	});

	pi.on("session_shutdown", async (_event, ctx) => {
		if (timer) {
			clearInterval(timer);
			timer = null;
		}
		ctx.ui.setStatus("quizforge", undefined);
	});

	// 想临时回到原生外观时用 /qf:plain，/qf:brand 换回来。
	pi.registerCommand("qf:plain", {
		description: "临时恢复默认外观（去掉 QuizForge 标志）",
		handler: async (_args, ctx) => {
			ctx.ui.setHeader(undefined);
			ctx.ui.setStatus("quizforge", undefined);
			ctx.ui.setTitle(`(plain) ${bankLabel()}`);
			ctx.ui.notify("已恢复默认外观；/reload 或重启可回到 QuizForge 外观", "info");
		},
	});

	pi.registerCommand("qf:brand", {
		description: "恢复 QuizForge 标志与状态栏",
		handler: async (_args, ctx) => {
			ctx.ui.setTitle(baseTitle());
			ctx.ui.setHeader((_tui, theme) => ({
				render(width: number): string[] {
					return renderHeader(theme, width, ctx.model?.id);
				},
				invalidate() {},
			}));
			ctx.ui.setStatus(
				"quizforge",
				ctx.ui.theme.fg("dim", ` ${BRAND} 题库 ${bankLabel()} · 权限 ${permissionLabel()}`),
			);
			ctx.ui.notify("QuizForge 外观已恢复", "info");
		},
	});

	pi.registerCommand("qf:status", {
		description: "显示 QuizForge 当前状态（题库 / 工作目录 / 权限 / 模型 / 会话）",
		handler: async (_args, ctx) => {
			const lines = [
				`${BRAND} 状态`,
				`题库      ：${bankLabel()}${process.env.QUIZFORGE_BANK ? `（${process.env.QUIZFORGE_BANK}）` : ""}`,
				`工作目录  ：${workdirLabel()}`,
				`权限档    ：${permissionLabel()}（QF_PERMISSION=${process.env.QF_PERMISSION || "standard"}）`,
				`模型      ：${ctx.model?.id || "未设置"}`,
				`会话      ：${pi.getSessionName() || "未命名"}`,
			];
			ctx.ui.notify(lines.join("\n"), "info");
		},
	});
}
