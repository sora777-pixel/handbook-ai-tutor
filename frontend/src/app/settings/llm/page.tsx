"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  AlertTriangle,
  Brain,
  CheckCircle2,
  Cpu,
  Database,
  Eye,
  FileText,
  HardDrive,
  KeyRound,
  Loader2,
  PlayCircle,
  Send,
  Server,
  ShieldAlert,
  Volume2,
  XCircle,
} from "lucide-react";
import { AppHeader } from "@/components/app-header";
import { EmbedSettings } from "@/components/embed-settings";
import { PathSettings } from "@/components/path-settings";
import { UsagePanel } from "@/components/usage-panel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { api, apiBase, useAuth } from "@/lib/api";
import type { LlmTestOut, RoutesOut } from "@/lib/types";

type Health = {
  status: string;
  llm_default_provider: string;
  llm_provider_text: string;
  llm_provider_audio: string;
  llm_provider_embed: string;
  task_backend: string;
  storage_backend: string;
  storage_location: string;
  db: string;
  stt_provider?: string;
  stt_providers?: string[];
};

const MODALITY_LABEL: Record<string, { label: string; icon: React.ReactNode }> = {
  text: { label: "文本", icon: <FileText className="h-3 w-3" /> },
  audio: { label: "语音", icon: <Volume2 className="h-3 w-3" /> },
  embed: { label: "向量", icon: <Database className="h-3 w-3" /> },
  vision: { label: "读图", icon: <Eye className="h-3 w-3" /> },
};

const TASKS = [
  "summarize",
  "extract_knowledge",
  "segment_summarize",
  "quiz_generate",
  "quiz_explain",
  "notes_generate",
  "tutor",
  "embed",
];

export default function LlmSettingsPage() {
  const token = useAuth((s) => s.token);
  const router = useRouter();
  const [task, setTask] = useState("summarize");
  const [prompt, setPrompt] = useState("Reply with the single word: ok");
  const [outcome, setOutcome] = useState<LlmTestOut | null>(null);

  useEffect(() => {
    if (!token) router.replace("/login");
  }, [token, router]);

  const health = useQuery({
    queryKey: ["health"],
    queryFn: async () => {
      const res = await fetch(`${apiBase}/health`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return (await res.json()) as Health;
    },
  });

  const routes = useQuery({
    queryKey: ["llm-routes"],
    queryFn: () => api<RoutesOut>("/api/v1/llm/routes"),
    enabled: !!token,
  });

  const test = useMutation({
    mutationFn: () =>
      api<LlmTestOut>("/api/v1/llm/test", {
        method: "POST",
        body: JSON.stringify({ task, prompt }),
      }),
    onSuccess: setOutcome,
    onError: (err) =>
      setOutcome({
        ok: false,
        task,
        modality: "—",
        provider: null,
        model: null,
        reply: null,
        error: err instanceof Error ? err.message : "调用失败",
      }),
  });

  const ocr = routes.data?.ocr;
  const ocrCoversVision = !!ocr?.ready;
  const missing = (routes.data?.routes || []).filter(
    (r) => !r.ready && !(r.modality === "vision" && ocrCoversVision)
  );
  const usingMock = (routes.data?.routes || []).every((r) => r.provider === "mock");

  return (
    <div className="min-h-screen">
      <AppHeader />
      <main className="mx-auto max-w-[1600px] space-y-6 px-4 pb-12 pt-6 sm:px-6">
        <PageHeader />

        {usingMock && (
          <AlertBlock
            variant="warning"
            icon={<AlertTriangle className="h-5 w-5" />}
            title="当前所有任务都路由到 mock（离线桩）"
            description={
              <>
                不会产生真实大模型调用。请在 <code>.env</code> 中填写至少一个 API KEY
                并重启后端。
              </>
            }
          />
        )}
        {!usingMock && missing.length > 0 && (
          <AlertBlock
            variant="destructive"
            icon={<ShieldAlert className="h-5 w-5" />}
            title={`有 ${missing.length} 个任务缺少可用密钥`}
            description={
              <>
                调用时会直接报错而不是静默降级：
                <ul className="mt-1.5 space-y-0.5 pl-4">
                  {missing.map((r) => (
                    <li key={r.task}>
                      <code>{r.task}</code> → {r.provider}
                      {r.api_key_env ? `（需要 ${r.api_key_env}）` : ""}
                    </li>
                  ))}
                </ul>
              </>
            }
          />
        )}

        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <KpiCard
            icon={<Server className="h-4 w-4" />}
            label="任务队列"
            value={health.data?.task_backend}
            loading={health.isLoading}
            tone="primary"
          />
          <KpiCard
            icon={<HardDrive className="h-4 w-4" />}
            label="存储后端"
            value={health.data?.storage_backend}
            loading={health.isLoading}
            tone="info"
          />
          <KpiCard
            icon={<Database className="h-4 w-4" />}
            label="数据库"
            value={health.data?.db}
            loading={health.isLoading}
            tone="success"
          />
          <KpiCard
            icon={<FileText className="h-4 w-4" />}
            label="文本任务供应商"
            value={health.data?.llm_provider_text || "（自动）"}
            loading={health.isLoading}
            tone="primary"
          />
          <KpiCard
            icon={<Volume2 className="h-4 w-4" />}
            label="语音任务供应商"
            value={health.data?.llm_provider_audio || "（自动）"}
            loading={health.isLoading}
            tone="info"
          />
          <KpiCard
            icon={<Database className="h-4 w-4" />}
            label="向量任务供应商"
            value={health.data?.llm_provider_embed || "（自动）"}
            loading={health.isLoading}
            tone="success"
          />
          <KpiCard
            icon={<Volume2 className="h-4 w-4" />}
            label="语音转写 STT"
            value={health.data?.stt_provider || "—"}
            loading={health.isLoading}
            tone="info"
          />
        </div>

        <PathSettings />

        <EmbedSettings />

        <Card className="overflow-hidden">
          <CardHeader className="pb-3">
            <div className="flex items-center gap-2">
              <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
                <Eye className="h-4 w-4" />
              </span>
              <div>
                <CardTitle className="text-base">图片 / 扫描版电子书读取（OCR）</CardTitle>
                <CardDescription>
                  上传 PNG / JPG 或没有文字层的扫描 PDF 时，靠这条链路转成文字，再进入知识点梳理与出题。
                </CardDescription>
              </div>
            </div>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            {!ocr ? (
              <p className="flex items-center gap-2 text-muted-foreground">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                加载中…
              </p>
            ) : (
              <>
                <div className="flex flex-wrap items-center gap-2">
                  {ocr.ready ? (
                    <Badge variant="success">
                      <CheckCircle2 className="h-3 w-3" />
                      可用
                    </Badge>
                  ) : (
                    <Badge variant="destructive">
                      <XCircle className="h-3 w-3" />
                      不可用
                    </Badge>
                  )}
                  <span className="text-muted-foreground">
                    引擎：<code>{ocr.engine}</code>（OCR_PROVIDER={ocr.mode}）
                  </span>
                </div>
                {ocr.installed_languages && ocr.installed_languages.length > 0 && (
                  <p className="text-muted-foreground">
                    已安装语言模型：{ocr.installed_languages.join(" / ")}（OCR_LANG={ocr.requested_language}）
                  </p>
                )}
                <p className="text-muted-foreground">
                  视觉大模型兜底：
                  {ocr.vision_fallback_ready ? (
                    <span className="ml-1 inline-flex items-center gap-1 text-success">
                      <CheckCircle2 className="h-3 w-3" />
                      已就绪
                    </span>
                  ) : (
                    <span className="ml-1">未启用 —— {ocr.vision_fallback_reason}</span>
                  )}
                </p>
                {!ocr.ready && ocr.reason && (
                  <p className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-destructive">
                    <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                    图片与扫描版 PDF 上传会失败：{ocr.reason}
                  </p>
                )}
              </>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <div className="flex items-center gap-2">
              <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
                <Cpu className="h-4 w-4" />
              </span>
              <div>
                <CardTitle className="text-base">任务 → 供应商映射</CardTitle>
                <CardDescription>
                  优先级：LLM_TASK_ROUTES → providers.yaml.task_routes → 模态变量 → LLM_DEFAULT_PROVIDER → mock
                </CardDescription>
              </div>
            </div>
          </CardHeader>
          <CardContent>
            {routes.isLoading && (
              <div className="space-y-2">
                {Array.from({ length: 6 }).map((_, i) => (
                  <div key={i} className="skeleton h-9 w-full" />
                ))}
              </div>
            )}
            {routes.data && (
              <div className="overflow-x-auto rounded-lg border">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b bg-muted/40 text-left text-xs uppercase tracking-wider text-muted-foreground">
                      <th className="px-3 py-2.5">任务</th>
                      <th className="px-3 py-2.5">模态</th>
                      <th className="px-3 py-2.5">供应商</th>
                      <th className="px-3 py-2.5">模型</th>
                      <th className="px-3 py-2.5">密钥变量</th>
                      <th className="px-3 py-2.5">状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {routes.data.routes.map((row, i) => (
                      <tr
                        key={row.task}
                        className={[
                          "border-b transition-colors last:border-0 hover:bg-muted/30",
                          i % 2 === 1 ? "bg-muted/10" : "",
                        ].join(" ")}
                      >
                        <td className="px-3 py-2 font-mono text-xs">{row.task}</td>
                        <td className="px-3 py-2">
                          {MODALITY_LABEL[row.modality] ? (
                            <span className="inline-flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 text-xs">
                              {MODALITY_LABEL[row.modality].icon}
                              {MODALITY_LABEL[row.modality].label}
                            </span>
                          ) : (
                            row.modality
                          )}
                        </td>
                        <td className="px-3 py-2 font-medium">{row.provider}</td>
                        <td className="px-3 py-2 text-muted-foreground">{row.model || "—"}</td>
                        <td className="px-3 py-2 font-mono text-xs">
                          {row.api_key_env || "—"}
                          {row.api_key_present === false && (
                            <Badge variant="destructive" className="ml-1">
                              缺失
                            </Badge>
                          )}
                          {row.api_key_present === true && (
                            <Badge variant="success" className="ml-1">
                              <KeyRound className="h-3 w-3" />
                              已读取
                            </Badge>
                          )}
                        </td>
                        <td className="px-3 py-2">
                          {row.ready ? (
                            <Badge variant={row.note ? "soft" : "success"}>
                              {row.note ? "已就绪*" : "已就绪"}
                            </Badge>
                          ) : (
                            <span className="text-xs text-destructive">{row.error || "不可用"}</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {routes.data.routes.some((r) => r.note) && (
                  <p className="border-t bg-muted/20 px-3 py-2 text-xs text-muted-foreground">
                    * {routes.data.routes.find((r) => r.note)?.note}
                  </p>
                )}
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <div className="flex items-center gap-2">
              <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-brand text-white">
                <PlayCircle className="h-4 w-4" />
              </span>
              <div>
                <CardTitle className="text-base">真实往返测试</CardTitle>
                <CardDescription>
                  发一个极小的请求给解析出的供应商，返回真实模型输出。用于排除「以为在用大模型，其实在用 mock」。
                </CardDescription>
              </div>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex flex-wrap gap-2">
              {TASKS.map((t) => (
                <Button
                  key={t}
                  size="sm"
                  variant={task === t ? "gradient" : "outline"}
                  onClick={() => setTask(t)}
                >
                  {t}
                </Button>
              ))}
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">提示词</label>
              <Input value={prompt} onChange={(e) => setPrompt(e.target.value)} />
            </div>
            <Button
              variant="gradient"
              onClick={() => test.mutate()}
              pending={test.isPending}
            >
              {test.isPending ? "调用中…" : "发送测试请求"}
              {!test.isPending && <Send />}
            </Button>
            {outcome && <OutcomeBlock outcome={outcome} />}
          </CardContent>
        </Card>

        <UsagePanel />
      </main>
    </div>
  );
}

function PageHeader() {
  return (
    <div className="space-y-1 animate-fade-up">
      <div className="flex items-center gap-2">
        <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-brand text-white shadow-soft">
          <Brain className="h-5 w-5" />
        </span>
        <h1 className="text-2xl font-semibold tracking-tight">模型路由诊断</h1>
      </div>
      <p className="text-sm text-muted-foreground">
        确认 <code>.env</code> 里配置的 API KEY 真正被后端读取，并看清每个任务实际调用的供应商与模型。
      </p>
    </div>
  );
}

function AlertBlock({
  variant,
  icon,
  title,
  description,
}: {
  variant: "warning" | "destructive";
  icon: React.ReactNode;
  title: string;
  description: React.ReactNode;
}) {
  const styles =
    variant === "warning"
      ? "border-warning/30 bg-warning/5 text-foreground"
      : "border-destructive/30 bg-destructive/5 text-foreground";
  const iconStyle =
    variant === "warning"
      ? "bg-warning/15 text-warning"
      : "bg-destructive/15 text-destructive";
  return (
    <div
      role="alert"
      className={[
        "flex items-start gap-3 rounded-xl border p-4 animate-fade-up",
        styles,
      ].join(" ")}
    >
      <span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg ${iconStyle}`}>
        {icon}
      </span>
      <div className="text-sm">
        <p className="font-medium">{title}</p>
        <div className="mt-1 leading-relaxed text-muted-foreground">{description}</div>
      </div>
    </div>
  );
}

function KpiCard({
  icon,
  label,
  value,
  loading,
  tone,
}: {
  icon: React.ReactNode;
  label: string;
  value: string | undefined;
  loading?: boolean;
  tone: "primary" | "info" | "success";
}) {
  const toneClass = {
    primary: "bg-primary/10 text-primary",
    info: "bg-info/10 text-info",
    success: "bg-success/10 text-success",
  }[tone];
  return (
    <Card>
      <CardContent className="space-y-2 p-5">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <span className={`flex h-6 w-6 items-center justify-center rounded-md ${toneClass}`}>
            {icon}
          </span>
          {label}
        </div>
        {loading ? (
          <div className="skeleton h-5 w-2/3" />
        ) : (
          <p className="break-all text-base font-semibold tracking-tight">{value ?? "—"}</p>
        )}
      </CardContent>
    </Card>
  );
}

function OutcomeBlock({ outcome }: { outcome: LlmTestOut }) {
  return (
    <div
      className={[
        "rounded-lg border p-4 text-sm animate-fade-up",
        outcome.ok
          ? "border-success/30 bg-success/5"
          : "border-destructive/30 bg-destructive/5",
      ].join(" ")}
    >
      <div className="flex flex-wrap items-center gap-2">
        {outcome.ok ? (
          <Badge variant="success">
            <CheckCircle2 className="h-3 w-3" />
            调用成功
          </Badge>
        ) : (
          <Badge variant="destructive">
            <XCircle className="h-3 w-3" />
            调用失败
          </Badge>
        )}
        <span className="text-muted-foreground">
          task=<code>{outcome.task}</code> · modality={outcome.modality} · provider={outcome.provider || "—"} · model={outcome.model || "—"}
        </span>
      </div>
      <div className="mt-2 rounded-md bg-card/50 p-3">
        {outcome.ok ? (
          <p className="whitespace-pre-wrap break-all">
            <span className="text-xs text-muted-foreground">模型回复：</span>
            <span className="font-medium">{outcome.reply}</span>
          </p>
        ) : (
          <p className="whitespace-pre-wrap break-all text-destructive">
            <span className="text-xs">错误：</span>
            {outcome.error}
          </p>
        )}
      </div>
    </div>
  );
}
