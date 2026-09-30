/**
 * `/api` —— 在对话里查看 / 测试 QuizForge 的 API 配置。
 *
 * 复用 MCP 工具（不重写业务逻辑）：
 *   list_api_configs  列出五类用途的全部条目（凭据永不回显）
 *   test_api_config   连通性测试
 *
 * 说明：新增或修改需要明文 Key/Token 的配置**不在这里做**——凭据不进对话，
 * 一律引导到桌面版设置页的安全输入框；本机端点（magpie / Ollama / LM Studio）
 * 可以让 agent 用 upsert_api_config 直接建。
 */
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const PREFIX = "mcp__quizforge__";

interface ApiRow {
	cid: string;
	name?: string;
	purposes?: string[];
	active?: boolean;
	protocol?: string;
	transport?: string;
	base_url?: string;
	model?: string;
	secret_state?: string;
}

function toolText(result: unknown): string {
	if (typeof result === "string") return result;
	const content = (result as { content?: Array<{ text?: string }> })?.content;
	if (Array.isArray(content) && content[0]?.text) return String(content[0].text);
	try {
		return JSON.stringify(result);
	} catch {
		return String(result);
	}
}

async function callTool(ctx: ExtensionContext, name: string, args: Record<string, unknown> = {}): Promise<string> {
	const runner = (ctx as unknown as { executeTool?: (n: string, a: unknown, o?: unknown) => Promise<unknown> }).executeTool;
	if (typeof runner !== "function") {
		throw new Error("当前 pi 版本不支持从扩展调用工具（ctx.executeTool 缺失）");
	}
	return toolText(await runner.call(ctx, `${PREFIX}${name}`, args));
}

const PURPOSE_LABEL: Record<string, string> = {
	agent: "Agent 对话",
	md: "题目识别 LLM",
	redraw: "配图重绘",
	"ocr-mineru": "OCR·MinerU",
	"ocr-doc2x": "OCR·Doc2X",
};

const SECRET_LABEL: Record<string, string> = {
	stored: "已存凭据",
	missing: "缺凭据",
	"not-required": "无需凭据",
};

function purposeText(row: ApiRow): string {
	const list = (row.purposes ?? []).map((p) => PURPOSE_LABEL[p] ?? p);
	if (!list.length) return "未启用";
	return list.join("、") + (row.active ? "（生效中）" : "");
}

export default function (pi: ExtensionAPI) {
	pi.registerCommand("api", {
		description: "查看 API 配置（列出五类用途的条目，可选用一条做连通性测试）",
		handler: async (_args: string, ctx: ExtensionContext) => {
			let rows: ApiRow[];
			try {
				const raw = await callTool(ctx, "list_api_configs");
				const parsed = JSON.parse(raw) as { configs?: ApiRow[] } | ApiRow[];
				rows = Array.isArray(parsed) ? parsed : (parsed.configs ?? []);
			} catch (error) {
				ctx.ui.notify(`读取 API 配置失败：${String(error)}`, "error");
				return;
			}
			if (!rows.length) {
				ctx.ui.notify(
					"还没有任何 API 配置。\n可以在桌面版「设置 → 模型 / OCR」添加，或对 magpie 这类本机端点直接说「配置 magpie」。",
					"warning",
				);
				return;
			}

			const labels = rows.map((row) => {
				const mark = row.active ? "●" : "○";
				const secret = SECRET_LABEL[String(row.secret_state ?? "")] ?? String(row.secret_state ?? "");
				return `${mark} ${row.name || row.cid}  [${purposeText(row)}]  ${row.model || ""}  ${secret}`;
			});

			const picked = await ctx.ui.select(
				`API 配置（共 ${rows.length} 条，↑↓ 移动 / Enter 测试连通性 / Esc 取消）`,
				labels,
			);
			if (!picked) return;
			const index = labels.indexOf(picked);
			if (index < 0) return;
			const row = rows[index];

			ctx.ui.notify(`正在测试 ${row.name || row.cid} …`, "info");
			try {
				const text = await callTool(ctx, "test_api_config", { cid: row.cid });
				ctx.ui.notify(`${row.name || row.cid}\n${text.slice(0, 600)}`, "info");
			} catch (error) {
				ctx.ui.notify(`测试失败：${String(error)}`, "error");
			}
		},
	});

	// 不进选择器，直接打印全部条目（适合贴给用户看）
	pi.registerCommand("api:list", {
		description: "直接列出全部 API 配置（脱敏）",
		handler: async (_args: string, ctx: ExtensionContext) => {
			try {
				const raw = await callTool(ctx, "list_api_configs");
				const parsed = JSON.parse(raw) as { configs?: ApiRow[] } | ApiRow[];
				const rows = Array.isArray(parsed) ? parsed : (parsed.configs ?? []);
				if (!rows.length) {
					ctx.ui.notify("还没有任何 API 配置", "warning");
					return;
				}
				const lines = rows.map((row) => {
					const secret = SECRET_LABEL[String(row.secret_state ?? "")] ?? "";
					return `${row.active ? "●" : "○"} ${row.cid}\n    ${row.name || ""} · ${purposeText(row)} · ${row.model || ""} · ${secret}`;
				});
				ctx.ui.notify(lines.join("\n"), "info");
			} catch (error) {
				ctx.ui.notify(`读取失败：${String(error)}`, "error");
			}
		},
	});
}
