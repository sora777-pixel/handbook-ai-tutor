"use client";

import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  BookOpen,
  CheckCircle2,
  ExternalLink,
  FileText,
  Film,
  Image as ImageIcon,
  Library,
  Loader2,
  Sparkles,
  Trash2,
  UploadCloud,
  X,
} from "lucide-react";
import { AppHeader } from "@/components/app-header";
import { ExtractionNote } from "@/components/extraction-note";
import {
  UploadDropzone,
  UploadQueue,
  uploadMany,
  type UploadRow,
} from "@/components/upload-batch";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { api, apiBase, streamTaskEvents, useAuth, type TaskEvent } from "@/lib/api";
import { formatBytes, formatTime } from "@/lib/io";
import type { Source, SourceDeleteReport } from "@/lib/types";

const STATUS_TONE: Record<
  string,
  { variant: "default" | "secondary" | "soft" | "warning" | "destructive" | "gradient"; label: string; dot: string }
> = {
  ready: { variant: "gradient", label: "就绪", dot: "bg-success" },
  processing: { variant: "soft", label: "解析中", dot: "bg-info animate-pulse-soft" },
  queued: { variant: "soft", label: "排队中", dot: "bg-info animate-pulse-soft" },
  failed: { variant: "destructive", label: "失败", dot: "bg-destructive" },
};

const KIND_ICON = {
  pdf: FileText,
  video: Film,
  image: ImageIcon,
};

export default function DashboardPage() {
  const token = useAuth((s) => s.token);
  const router = useRouter();
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [queue, setQueue] = useState<UploadRow[]>([]);
  const [pendingDelete, setPendingDelete] = useState<Source | null>(null);
  const [lastReport, setLastReport] = useState<SourceDeleteReport | null>(null);
  const [liveTasks, setLiveTasks] = useState<Record<string, TaskEvent>>({});
  const [streaming, setStreaming] = useState(false);

  useEffect(() => {
    if (!token) router.replace("/login");
  }, [token, router]);

  const sources = useQuery({
    queryKey: ["sources"],
    queryFn: () => api<Source[]>("/api/v1/sources"),
    enabled: !!token,
    // Poll only while something is still being parsed. An unconditional 2s
    // interval re-rendered the whole grid (and restarted its entrance
    // animations) forever, which is what made the shelf feel sticky to scroll
    // and made the cards twitch under the cursor.
    refetchInterval: (q) => {
      const rows = q.state.data;
      if (!rows || rows.length === 0) return false;
      const busy = rows.some((s) => s.status !== "ready" && s.status !== "failed");
      // Live SSE replaces the 2s poll when the stream is up.
      return busy && !streaming ? 2000 : false;
    },
  });

  const busyTaskIds = (sources.data || [])
    .filter((s) => s.status !== "ready" && s.status !== "failed" && s.task_id)
    .map((s) => s.task_id as string)
    .sort()
    .join(",");

  useEffect(() => {
    if (!token || !busyTaskIds) {
      setStreaming(false);
      return;
    }
    const ids = busyTaskIds.split(",");
    const ac = new AbortController();
    let active = false;
    for (const taskId of ids) {
      void streamTaskEvents(
        taskId,
        (event) => {
          active = true;
          setStreaming(true);
          setLiveTasks((prev) => ({ ...prev, [event.source_id]: event }));
          if (event.status === "succeeded" || event.status === "failed") {
            void queryClient.invalidateQueries({ queryKey: ["sources"] });
          }
        },
        ac.signal
      ).then((result) => {
        if (result === "failed" && !active) setStreaming(false);
      });
    }
    return () => ac.abort();
  }, [busyTaskIds, token, queryClient]);

  const remove = useMutation({
    mutationFn: (source: Source) =>
      api<SourceDeleteReport>(`/api/v1/sources/${source.id}?delete_files=true`, { method: "DELETE" }),
    onSuccess: (report) => {
      setLastReport(report);
      setPendingDelete(null);
      setError(null);
      void queryClient.invalidateQueries({ queryKey: ["sources"] });
    },
    onError: (err) => {
      setPendingDelete(null);
      setError(err instanceof Error ? err.message : "删除失败");
    },
  });

  /**
   * Several files at once. Each one is POSTed independently and reports its own
   * verdict, so a rejected file never cancels the rest. A single file keeps the
   * old behaviour (jump straight into it); a batch stays here, where the queue
   * below and the source list show what is happening.
   */
  async function onFiles(files: File[]) {
    if (!files.length) return;
    setError(null);
    setUploading(true);
    const single = files.length === 1;
    if (single) setQueue([]);
    try {
      const outcome = await uploadMany(files, (row) => {
        setQueue((prev) => {
          const rest = prev.filter((r) => r.id !== row.id);
          return single && row.state !== "accepted" ? prev : [...rest, row];
        });
      });
      if (single && outcome.lastSourceId) {
        router.push(`/sources/${outcome.lastSourceId}`);
        return;
      }
      if (outcome.failed > 0) {
        setError(`${outcome.ok} 个已进入解析，${outcome.failed} 个失败：见下方列表。`);
      }
      queryClient.invalidateQueries({ queryKey: ["sources"] });
    } finally {
      setUploading(false);
    }
  }

  return (
    <div className="min-h-screen">
      <AppHeader />
      <main className="mx-auto max-w-[1600px] space-y-8 px-4 pb-12 pt-6 sm:px-6">
        <Hero />

        <Card
          className="overflow-hidden border-border/70 animate-fade-up"
          style={{ animationDelay: "0.05s" }}
        >
          <CardHeader className="pb-3">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <CardTitle className="text-lg">上传学习资料</CardTitle>
                <CardDescription className="mt-1">
                  PDF / MP4 / EPUB / 图片。上传后进入异步流水线：解析 → 知识点梳理 → 可检索索引。
                </CardDescription>
              </div>
              <Badge variant="soft">
                <Sparkles className="h-3 w-3" />
                支持 .pdf / .mp4 / .epub / .png / .jpg
              </Badge>
            </div>
          </CardHeader>
          <CardContent>
            <UploadDropzone busy={uploading} onFiles={(files) => void onFiles(files)} />

            {queue.length > 0 && (
              <div className="mt-3">
                <UploadQueue rows={queue} />
              </div>
            )}
            {error && (
              <div
                role="alert"
                className="mt-3 flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive animate-fade-in"
              >
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                <span className="leading-relaxed break-all">{error}</span>
              </div>
            )}
          </CardContent>
        </Card>

        {lastReport && (
          <Card
            className="border-primary/30 bg-primary/[0.03] animate-fade-up"
            style={{ animationDelay: "0.1s" }}
          >
            <CardHeader className="pb-2">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <CardTitle className="text-base">上次删除结果</CardTitle>
                  <CardDescription className="mt-1">
                    {lastReport.filename} · 共清理 {lastReport.total_rows_deleted} 条数据库记录
                  </CardDescription>
                </div>
                <Button size="icon" variant="ghost" onClick={() => setLastReport(null)} aria-label="收起">
                  <X />
                </Button>
              </div>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              <p className="flex items-center gap-2">
                存储文件：
                {lastReport.storage_deleted ? (
                  <span className="inline-flex items-center gap-1 text-success">
                    <CheckCircle2 className="h-4 w-4" />
                    已从本地磁盘删除
                  </span>
                ) : (
                  <span className="text-destructive">
                    未删除{lastReport.storage_error ? `（${lastReport.storage_error}）` : ""}
                  </span>
                )}
              </p>
              <p className="text-muted-foreground">
                存储键：<code className="break-all">{lastReport.storage_key}</code>
              </p>
              <div className="flex flex-wrap gap-2 text-xs">
                {Object.entries(lastReport.rows_deleted).map(([table, count]) => (
                  <Badge key={table} variant="secondary">
                    {table}: {count}
                  </Badge>
                ))}
              </div>
            </CardContent>
          </Card>
        )}

        <section className="space-y-4 animate-fade-up" style={{ animationDelay: "0.15s" }}>
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div>
              <h2 className="flex items-center gap-2 text-lg font-semibold">
                <Library className="h-5 w-5 text-primary" />
                我的书架
              </h2>
              <p className="text-sm text-muted-foreground">
                {sources.data?.length
                  ? `共 ${sources.data.length} 本 / 段资料`
                  : "上传一份资料开始你的学习"}
              </p>
            </div>
          </div>

          {sources.isLoading && (
            <div className="grid gap-4 md:grid-cols-2">
              {[0, 1, 2, 3].map((i) => (
                <div key={i} className="h-40 rounded-xl border bg-card shadow-soft">
                  <div className="space-y-3 p-5">
                    <div className="skeleton h-5 w-3/4" />
                    <div className="skeleton h-3 w-1/2" />
                    <div className="skeleton h-3 w-full" />
                    <div className="flex gap-2 pt-2">
                      <div className="skeleton h-8 w-20" />
                      <div className="skeleton h-8 w-16" />
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}

          {!sources.isLoading && sources.data && sources.data.length > 0 && (
            <div className="grid gap-4 md:grid-cols-2">
              {sources.data.map((s) => (
                <SourceCard
                  key={s.id}
                  source={s}
                  token={token}
                  live={liveTasks[s.id]}
                  onDelete={() => setPendingDelete(s)}
                  deleting={remove.isPending}
                />
              ))}
            </div>
          )}

          {!sources.isLoading && sources.data?.length === 0 && (
            <EmptyShelf />
          )}
        </section>
      </main>

      {pendingDelete && (
        <DeleteConfirm
          source={pendingDelete}
          pending={remove.isPending}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => remove.mutate(pendingDelete)}
        />
      )}
    </div>
  );
}

function Hero() {
  return (
    <div className="relative overflow-hidden rounded-2xl border border-border/60 bg-gradient-brand p-6 text-white shadow-lift sm:p-8 animate-fade-up">
      <div className="absolute inset-0 opacity-30">
        <div className="absolute -right-12 -top-12 h-56 w-56 rounded-full bg-white/20 blur-3xl" />
        <div className="absolute -bottom-16 -left-12 h-72 w-72 rounded-full bg-white/10 blur-3xl" />
      </div>
      <div className="relative flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <Badge variant="gradient" className="bg-white/20 text-white">
            <Sparkles className="h-3 w-3" />
            智能学习工作台
          </Badge>
          <h1 className="mt-3 text-2xl font-semibold tracking-tight sm:text-3xl">
            开始你今天的学习吧
          </h1>
          <p className="mt-1.5 max-w-xl text-sm text-white/85 sm:text-base">
            上传资料后，AI 会自动做摘要、按章节出题、并像老师一样回答你的疑问。
          </p>
        </div>
        <div className="hidden gap-2 sm:flex">
          <KpiChip icon={<BookOpen className="h-4 w-4" />} label="已收录知识点" />
          <KpiChip icon={<FileText className="h-4 w-4" />} label="可检索切片" />
        </div>
      </div>
    </div>
  );
}

function KpiChip({ icon, label }: { icon: React.ReactNode; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full bg-white/15 px-3 py-1 text-xs backdrop-blur-sm">
      {icon}
      {label}
    </span>
  );
}

function SourceCard({
  source,
  token,
  live,
  onDelete,
  deleting,
}: {
  source: Source;
  token: string | null;
  live?: TaskEvent;
  onDelete: () => void;
  deleting: boolean;
}) {
  const tone = STATUS_TONE[source.status] ?? {
    variant: "secondary" as const,
    label: source.status,
    dot: "bg-muted-foreground",
  };
  const Icon = KIND_ICON[source.kind as keyof typeof KIND_ICON] ?? FileText;
  // The API titles a recording with its file name (a generated heading must not
  // masquerade as the file's name), so the heading already says everything for a
  // video. A document shows its file name as a second line only when that adds
  // information the model-written title does not carry.
  const heading = source.title || source.filename;
  const subtitle = source.filename && source.filename !== heading ? source.filename : null;
  const progress = live?.progress ?? source.task_progress;
  const step = live?.step ?? source.task_step;
  const note = live?.message ?? source.extraction_note ?? source.task_message;
  const busy = source.status !== "ready" && source.status !== "failed";
  return (
    <Card interactive className="group flex h-full flex-col justify-between">
      <Link href={`/sources/${source.id}`} className="block">
        <CardHeader>
          <div className="flex items-start justify-between gap-2">
            <div className="flex min-w-0 items-start gap-3">
              <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary transition-colors group-hover:bg-primary group-hover:text-primary-foreground">
                <Icon className="h-5 w-5" />
              </span>
              <div className="min-w-0">
                <CardTitle className="line-clamp-1 text-base" title={heading}>
                  {heading}
                </CardTitle>
                <p className="mt-0.5 line-clamp-1 text-xs text-muted-foreground">
                  {source.kind.toUpperCase()} · {formatBytes(source.byte_size)} ·{" "}
                  {formatTime(source.created_at)}
                </p>
                {/* Kept as an empty line rather than removed: the card sits in a
                    grid and a missing row makes the recordings look shorter. */}
                <p className="mt-1 line-clamp-1 text-xs text-muted-foreground/80">
                  {subtitle ?? "\u00A0"}
                </p>
              </div>
            </div>
            <Badge variant={tone.variant}>
              <span className={`status-dot ${tone.dot}`} />
              {tone.label}
            </Badge>
          </div>
        </CardHeader>
      </Link>
      <CardContent className="space-y-3 pt-0">
        {busy && (
          <div className="space-y-1">
            <Progress value={progress ?? 5} />
            <p className="text-xs text-muted-foreground">
              {step || source.status}
              {live?.message ? ` · ${live.message}` : ""}
            </p>
          </div>
        )}
        {note && <ExtractionNote note={note} compact />}
        <div className="flex items-center justify-between gap-2">
        <Button asChild size="sm" variant="ghost" className="text-muted-foreground">
          <a href={`${apiBase}/api/v1/sources/${source.id}/file${token ? `?token=${token}` : ""}`}>
            <ExternalLink />
            原文件
          </a>
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={onDelete}
          disabled={deleting}
          className="text-destructive hover:bg-destructive/10 hover:text-destructive"
        >
          <Trash2 />
          删除
        </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function EmptyShelf() {
  return (
    <div className="rounded-xl border border-dashed bg-card/50 p-10 text-center">
      <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl bg-gradient-brand text-white shadow-soft">
        <BookOpen className="h-7 w-7" />
      </div>
      <p className="mt-4 text-base font-medium">书架还是空的</p>
      <p className="mt-1 text-sm text-muted-foreground">
        先上传一份 PDF、一张教材截图或一段短视频试试。
      </p>
    </div>
  );
}

function DeleteConfirm({
  source,
  pending,
  onCancel,
  onConfirm,
}: {
  source: Source;
  pending: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-foreground/40 p-4 backdrop-blur-sm animate-fade-in"
      onClick={onCancel}
    >
      <Card
        className="w-full max-w-md shadow-lift animate-scale-in"
        onClick={(e) => e.stopPropagation()}
      >
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Trash2 className="h-4 w-4 text-destructive" />
            确认删除《{source.title || source.filename}》？
          </CardTitle>
          <CardDescription>此操作不可撤销。</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4 text-sm">
          <p>将同时删除：</p>
          <ul className="space-y-1.5 pl-1 text-muted-foreground">
            {[
              "本地磁盘 / MinIO 中保存的原始文件",
              "AI 生成的摘要与知识点梳理",
              "你的全部学习笔记",
              "Quiz 题目、作答记录与解析",
              "切片索引、对话记录与用量统计",
            ].map((item) => (
              <li key={item} className="flex items-start gap-2">
                <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-muted-foreground" />
                {item}
              </li>
            ))}
          </ul>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={onCancel} disabled={pending}>
              取消
            </Button>
            <Button variant="destructive" onClick={onConfirm} pending={pending}>
              {pending ? "删除中…" : "确认删除"}
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
