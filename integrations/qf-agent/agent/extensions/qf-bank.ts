/**
 * `/bank` —— 在对话里用上下键切换题库。
 *
 * 题库清单来自桌面版登记（%LOCALAPPDATA%\QuizForge\desktop.json），
 * 选择结果写回本 agent 目录的 mcp.json（quizforge.env），与 `qf-bank` 命令共用同一份配置。
 *
 * 注意：MCP server 是会话启动时长驻的子进程，题库是它的启动环境，
 * 所以切换后当前会话需要 /mcp reconnect quizforge 才生效（下次启动 qf 自动生效）。
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

interface BankEntry {
	name: string;
	path: string;
	subject: string;
}

/** agent 目录：qf.cmd 会设置 PI_CODING_AGENT_DIR。 */
function agentDir(): string {
	const fromEnv = (process.env.QUIZFORGE_AGENT_DIR || process.env.PI_CODING_AGENT_DIR || "").trim();
	if (fromEnv) return fromEnv;
	const local = process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local");
	return path.join(local, "QuizForge", "agent");
}

function readJson<T>(file: string): T | null {
	try {
		return JSON.parse(fs.readFileSync(file, "utf8")) as T;
	} catch {
		return null;
	}
}

function desktopConfigPath(): string {
	const local = process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local");
	return path.join(local, "QuizForge", "desktop.json");
}

function normalise(p: string): string {
	return String(p || "").replace(/[\\/]+$/, "");
}

function listBanks(): { banks: BankEntry[]; assetsDir: string } {
	const desktop = readJson<any>(desktopConfigPath());
	const banks: BankEntry[] = [];
	let assetsDir = "";
	if (!desktop) return { banks, assetsDir };
	if (desktop.assets_dir) assetsDir = String(desktop.assets_dir);
	if (Array.isArray(desktop.banks)) {
		for (const item of desktop.banks) {
			if (!item || !item.path) continue;
			banks.push({
				name: String(item.name || path.basename(String(item.path))),
				path: String(item.path),
				subject: item.subject ? String(item.subject) : "math",
			});
		}
	}
	if (banks.length === 0 && desktop.bank_dir) {
		banks.push({
			name: path.basename(String(desktop.bank_dir)),
			path: String(desktop.bank_dir),
			subject: "math",
		});
	}
	return { banks, assetsDir };
}

function currentBankPath(): string {
	const mcp = readJson<any>(path.join(agentDir(), "mcp.json"));
	const value = mcp?.mcpServers?.quizforge?.env?.QUIZFORGE_BANK;
	return value ? String(value) : "";
}

function currentSubject(): string {
	const mcp = readJson<any>(path.join(agentDir(), "mcp.json"));
	const value = mcp?.mcpServers?.quizforge?.env?.QUIZFORGE_SUBJECT;
	return value ? String(value) : "math";
}

function subjectLabel(subject: string): string {
	return subject === "physics" ? "物理" : "数学";
}

function writeBank(bank: BankEntry, assetsDir: string): void {
	const file = path.join(agentDir(), "mcp.json");
	const mcp = readJson<any>(file) ?? {};
	mcp.mcpServers = mcp.mcpServers ?? {};
	const server = mcp.mcpServers.quizforge ?? {};
	server.env = {
		...(server.env ?? {}),
		QF_MODE: server.env?.QF_MODE ?? "danger",
		QUIZFORGE_BANK: bank.path,
		QUIZFORGE_SUBJECT: bank.subject || "math",
	};
	if (assetsDir) server.env.QUIZFORGE_ASSETS_DIR = assetsDir;
	mcp.mcpServers.quizforge = server;
	fs.writeFileSync(file, JSON.stringify(mcp, null, 2), "utf8");
}

export default function (pi: ExtensionAPI) {
	pi.registerCommand("bank", {
		description: "切换题库（上下键选择，回车确认）",
		handler: async (_args: string, ctx: ExtensionContext) => {
			const { banks, assetsDir } = listBanks();
			if (banks.length === 0) {
				ctx.ui.notify(
					"没有找到题库登记。请先用 `qf-bank <路径>` 指定，或确认桌面版已添加题库。",
					"warning",
				);
				return;
			}

			const current = normalise(currentBankPath());
			const labels = banks.map((bank) => {
				const mark = current && normalise(bank.path) === current ? "●" : "○";
				return `${mark} ${bank.name}  [${subjectLabel(bank.subject)}]  ${bank.path}`;
			});

			const picked = await ctx.ui.select("选择题库（↑↓ 移动，Enter 确认，Esc 取消）", labels);
			if (!picked) return;

			const index = labels.indexOf(picked);
			if (index < 0) return;
			const bank = banks[index];

			if (current && normalise(bank.path) === current) {
				ctx.ui.notify(`当前已经是「${bank.name}」`, "info");
				return;
			}

			try {
				writeBank(bank, assetsDir);
			} catch (error) {
				ctx.ui.notify(`写入 mcp.json 失败：${String(error)}`, "error");
				return;
			}

			ctx.ui.notify(
				[
					`已切换题库：${bank.name}`,
					`  ${bank.path}`,
					`  科目 ${subjectLabel(bank.subject)}${assetsDir ? `，共享图片 ${assetsDir}` : ""}`,
					"",
					"本会话需要重连 MCP 才生效：输入 /mcp reconnect quizforge",
					"（下次启动 qf 会自动使用新题库）",
				].join("\n"),
				"info",
			);
		},
	});

	// 非交互也可以查：/bank:status
	pi.registerCommand("bank:status", {
		description: "显示当前题库（不弹选择器）",
		handler: async (_args: string, ctx: ExtensionContext) => {
			const current = currentBankPath();
			if (!current) {
				ctx.ui.notify("当前未指定题库（QuizForge 会用默认目录）", "warning");
				return;
			}
			ctx.ui.notify(
				`当前题库：${path.basename(current)}\n  ${current}\n  科目 ${subjectLabel(currentSubject())}`,
				"info",
			);
		},
	});
}
