"use client";

import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  Download,
  ListChecks,
  Loader2,
  MessageSquareQuote,
  RefreshCw,
  Sparkles,
  Trash2,
  Upload,
} from "lucide-react";
import { AppHeader } from "@/components/app-header";
import { ExtractionNote } from "@/components/extraction-note";
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
import { Progress } from "@/components/ui/progress";
import { SourcePreview, type SourcePreviewHandle } from "@/components/source-preview";
import { StructurePanel } from "@/components/structure-panel";
import { Textarea } from "@/components/ui/textarea";
import { api, streamTaskEvents, useAuth } from "@/lib/api";
import {
  authedUrl,
  downloadJson,
  formatBytes,
  formatTime,
  pickJsonFile,
  slugify,
} from "@/lib/io";
import type {
  ImportResult,
  Knowledge,
  Note,
  RegenerateOut,
  Source,
  SourceBundle,
  SourceDeleteReport,
  Summary,
  Task,
} from "@/lib/types";

export default function SourcePage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const token = useAuth((s) => s.token);
  const router = useRouter();
  const queryClient = useQueryClient();
  const [banner, setBanner] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [liveTask, setLiveTask] = useState<Task | null>(null);
  const [streaming, setStreaming] = useState(false);

  useEffect(() => {
    if (!token) router.replace("/login");
  }, [token, router]);

  const source = useQuery({
    queryKey: ["source", id],
    queryFn: () => api<Source>(`/api/v1/sources/${id}`),
    enabled: !!token && !!id,
    // Poll *only* while the ingest pipeline is still moving. A fixed 1.5s
    // interval kept firing forever after the source reached a terminal state,
    // which re-rendered the whole page (video element included) twice a second
    // and made scrolling stutter for no reason.
    refetchInterval: (q) => {
      const status = q.state.data?.status;
      if (!status || status === "ready" || status === "failed") return false;
      return streaming ? false : 1500;
    },
  });

  const taskId = source.data?.task_id;
  const statusForPoll = source.data?.status;
  const busy = !!statusForPoll && statusForPoll !== "ready" && statusForPoll !== "failed";
  const task = useQuery({
    queryKey: ["task", taskId],
    queryFn: () => api<Task>(`/api/v1/tasks/${taskId}`),
    enabled: !!token && !!taskId,
    refetchInterval: (q) => {
      if (streaming) return false;
      return q.state.data?.status === "succeeded" || q.state.data?.status === "failed" ? false : 1000;
    },
  });

  useEffect(() => {
    if (!token || !taskId || !busy) {
      setStreaming(false);
      return;
    }
    const ac = new AbortController();
    let gotEvent = false;
    void streamTaskEvents(
      taskId,
      (event) => {
        gotEvent = true;
        setStreaming(true);
        setLiveTask({
          id: event.id,
          source_id: event.source_id,
          kind: "ingest",
          status: event.status,
          progress: event.progress,
          step: event.step,
          message: event.message,
        });
        if (event.status === "succeeded" || event.status === "failed") {
          void queryClient.invalidateQueries({ queryKey: ["source", id] });
          void queryClient.invalidateQueries({ queryKey: ["task", taskId] });
        }
      },
      ac.signal
    ).then((result) => {
      if (result === "failed" && !gotEvent) setStreaming(false);
    });
    return () => ac.abort();
  }, [token, taskId, busy, id, queryClient]);

  const taskView = liveTask || task.data;

  const status = source.data?.status;
  const ready = status === "ready";
  const failed = status === "failed";

  const summary = useQuery({
    queryKey: ["summary", id],
    queryFn: () => api<Summary>(`/api/v1/sources/${id}/summary`),
    enabled: !!token && ready,
    retry: false,
  });

  const knowledge = useQuery({
    queryKey: ["knowledge", id],
    queryFn: () => api<Knowledge[]>(`/api/v1/sources/${id}/knowledge`),
    enabled: !!token && ready,
  });

  const notes = useQuery({
    queryKey: ["notes", id],
    queryFn: () => api<Note[]>(`/api/v1/sources/${id}/notes`),
    enabled: !!token && !!id,
  });

  const regenerate = useMutation({
    mutationFn: () => api<RegenerateOut>(`/api/v1/sources/${id}/regenerate`, { method: "POST" }),
    onSuccess: (out) => {
      setBanner({ kind: "ok", text: `已用 ${out.provider}/${out.model} 重新生成梳理，共 ${out.knowledge_points} 个知识点` });
      void queryClient.invalidateQueries({ queryKey: ["summary", id] });
      void queryClient.invalidateQueries({ queryKey: ["knowledge", id] });
      void queryClient.invalidateQueries({ queryKey: ["source", id] });
    },
    onError: (err) => setBanner({ kind: "err", text: err instanceof Error ? err.message : "重新生成失败" }),
  });

  const generateNotes = useMutation({
    mutationFn: () => api<Note[]>(`/api/v1/sources/${id}/notes/generate`, { method: "POST" }),
    onSuccess: (created) => {
      setBanner({ kind: "ok", text: `AI 已生成 ${created.length} 条笔记草稿` });
      void queryClient.invalidateQueries({ queryKey: ["notes", id] });
    },
    onError: (err) => setBanner({ kind: "err", text: err instanceof Error ? err.message : "生成笔记失败" }),
  });

  const doExport = useMutation({
    mutationFn: () => api<SourceBundle>(`/api/v1/sources/${id}/export`),
    onSuccess: (bundle) => {
      const stem = slugify(source.data?.title || source.data?.filename, "bundle");
      downloadJson(`${stem}-bundle.json`, bundle);
      setBanner({
        kind: "ok",
        text: `已导出：${bundle.notes.length} 条笔记 / ${bundle.knowledge.length} 个知识点 / ${bundle.records.length} 条提交记录`,
      });
    },
    onError: (err) => setBanner({ kind: "err", text: err instanceof Error ? err.message : "导出失败" }),
  });

  const doImport = useMutation({
    mutationFn: async (mode: "merge" | "replace") => {
      const bundle = await pickJsonFile();
      return api<ImportResult>(`/api/v1/sources/${id}/import`, {
        method: "POST",
        body: JSON.stringify({ bundle, mode }),
      });
    },
    onSuccess: (res) => {
      setBanner({
        kind: "ok",
        text: `导入完成（${res.mode}）：新增笔记 ${res.notes_created}、跳过重复 ${res.notes_skipped}、恢复知识点 ${res.knowledge_restored}`,
      });
      void queryClient.invalidateQueries({ queryKey: ["notes", id] });
      void queryClient.invalidateQueries({ queryKey: ["knowledge", id] });
      void queryClient.invalidateQueries({ queryKey: ["summary", id] });
    },
    onError: (err) => setBanner({ kind: "err", text: err instanceof Error ? err.message : "导入失败" }),
  });

  const [confirmDelete, setConfirmDelete] = useState(false);
  const removeSource = useMutation({
    mutationFn: () =>
      api<SourceDeleteReport>(`/api/v1/sources/${id}?delete_files=true`, { method: "DELETE" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["sources"] });
      router.push("/dashboard");
    },
    onError: (err) => setBanner({ kind: "err", text: err instanceof Error ? err.message : "删除失败" }),
  });

  const fileUrl = useMemo(
    () => (id && token ? authedUrl(`/api/v1/sources/${id}/file`) : ""),
    [id, token]
  );

  const kind = source.data?.kind;
  const isVideo = kind === "video";

  // The API guarantees a recording is titled by its file name (the regenerate
  // pass used to overwrite it with a generated heading like 「无结构片段摘录」),
  // so this is just the loading fallback.
  const displayTitle = source.data?.title || source.data?.filename;

  const previewRef = useRef<SourcePreviewHandle>(null);

  const seekVideo = useCallback((seconds: number) => {
    previewRef.current?.seek(seconds);
  }, []);

  const jumpToPage = useCallback((page: number) => {
    previewRef.current?.jumpToPage(page);
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const page = params.get("page");
    const time = params.get("t");
    if (page) {
      const n = Number(page);
      if (Number.isFinite(n) && n > 0) jumpToPage(n);
    }
    if (time) {
      const seconds = Number(time);
      if (Number.isFinite(seconds)) seekVideo(seconds);
    }
  }, [seekVideo, jumpToPage, fileUrl]);

  return (
    <div className="flex min-h-screen flex-col">
      <AppHeader />

      <div className="mx-auto w-full max-w-[1600px] shrink-0 px-4 pt-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <h1 className="truncate text-xl font-semibold" title={displayTitle || undefined}>
              {displayTitle || "加载中…"}
            </h1>
            <p className="truncate text-xs text-muted-foreground">
              {isVideo ? "MP4 视频" : kind === "image" ? "图片" : "PDF"} ·{" "}
              {source.data ? formatBytes(source.data.byte_size) : "—"} ·{" "}
              <StatusBadge status={status} />
            </p>
            {!isVideo && source.data?.filename && (
              <p className="truncate text-xs text-muted-foreground/80" title={source.data.filename}>
                {source.data.filename}
              </p>
            )}
            {isVideo && summary.data?.title && (
              <p className="truncate text-xs text-muted-foreground/80" title={summary.data.title}>
                AI 主题：{summary.data.title}
              </p>
            )}
          </div>
          <div className="flex flex-wrap gap-2">
            <Button asChild size="sm" variant="secondary">
              <Link href={`/sources/${id}/quiz`} title="生成测试题与题目解析">
                <ListChecks />
                Quiz
              </Link>
            </Button>
            <Button asChild size="sm" variant="secondary">
              <Link href={`/sources/${id}/tutor`} title="就这份资料提问">
                <MessageSquareQuote />
                Tutor Q&A
              </Link>
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => regenerate.mutate()}
              disabled={!ready || regenerate.isPending}
              title={
                ready
                  ? "用当前配置的 LLM 重新生成摘要与知识点"
                  : failed
                    ? "解析失败，无法重新梳理；请修正配置后重新导入"
                    : "等待解析完成后可用"
              }
            >
              {regenerate.isPending ? <Loader2 className="animate-spin" /> : <RefreshCw />}
              {regenerate.isPending ? "重新生成中…" : "LLM 重新生成"}
            </Button>
            <Button size="sm" variant="outline" onClick={() => doExport.mutate()} disabled={doExport.isPending}>
              <Download />
              导出
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => doImport.mutate("merge")}
              disabled={doImport.isPending}
            >
              <Upload />
              导入
            </Button>
            <Button size="sm" variant="destructive" onClick={() => setConfirmDelete(true)}>
              <Trash2 />
              删除
            </Button>
          </div>
        </div>

        {banner && (
          <div
            className={`mt-3 flex items-start justify-between gap-3 rounded-lg border px-3 py-2.5 text-sm animate-fade-down ${
              banner.kind === "ok"
                ? "border-success/30 bg-success/5 text-foreground"
                : "border-destructive/30 bg-destructive/5 text-foreground"
            }`}
          >
            <div className="flex items-start gap-2">
              {banner.kind === "ok" ? (
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success" />
              ) : (
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
              )}
              <span className="break-all leading-relaxed">{banner.text}</span>
            </div>
            <button
              className="shrink-0 text-xs text-muted-foreground underline-offset-4 hover:underline"
              onClick={() => setBanner(null)}
            >
              关闭
            </button>
          </div>
        )}

        {!ready && (
          <Card className="mt-3 overflow-hidden">
            <CardHeader className="pb-3">
              <div className="flex items-center gap-2">
                <span className="flex h-7 w-7 items-center justify-center rounded-md bg-gradient-brand text-white">
                  <Sparkles className="h-3.5 w-3.5" />
                </span>
                <div>
                  <CardTitle className="text-sm">流水线进度</CardTitle>
                  <CardDescription>
                    {taskView?.step || status || "…"}
                    {taskView?.message ? ` · ${taskView.message}` : ""}
                  </CardDescription>
                </div>
              </div>
            </CardHeader>
            <CardContent>
              <Progress value={taskView?.progress ?? (ready ? 100 : 5)} />
              {failed && source.data?.error_message && (
                <p className="mt-2 flex items-start gap-2 text-sm text-destructive break-all">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                  {source.data.error_message}
                </p>
              )}
            </CardContent>
          </Card>
        )}

        {ready && (source.data?.extraction_note || source.data?.task_message) && (
          <div className="mt-3">
            <ExtractionNote note={source.data.extraction_note || source.data.task_message} />
          </div>
        )}
      </div>

      {/* Two independent panes: left = original document, right = AI notes + your notes. */}
      <main className="mx-auto grid w-full max-w-[1600px] flex-1 grid-cols-1 items-start gap-4 px-4 py-4 lg:grid-cols-2">
        <SourcePreview
          ref={previewRef}
          fileUrl={fileUrl}
          kind={kind}
          filename={source.data?.filename}
          sourceId={id}
          onError={(text) => setBanner({ kind: "err", text })}
        />

        <section className="flex max-h-[min(72vh,56rem)] min-h-[32rem] flex-col gap-4 overflow-y-auto pr-1">
          <Card>
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between gap-2">
                <CardTitle className="text-base">AI 知识点梳理</CardTitle>
                <Badge variant="secondary">
                  {summary.data?.prompt_version || (ready ? "—" : "等待中")}
                </Badge>
              </div>
              <CardDescription>
                自动摘要与分节知识点，可导出为 JSON 后在其他设备导入，也可让 LLM 重新生成。
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              {summary.data ? (
                <>
                  <div>
                    <p className="text-sm font-semibold">{summary.data.title}</p>
                    <p className="mt-1 whitespace-pre-wrap text-sm leading-6">{summary.data.overview}</p>
                  </div>
                  {summary.data.outline.length > 0 && (
                    <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
                      {summary.data.outline.map((item) => (
                        <li key={item}>{item}</li>
                      ))}
                    </ul>
                  )}
                </>
              ) : (
                <p className="text-sm text-muted-foreground">
                  {ready ? "暂无摘要。" : "解析完成后自动出现。"}
                </p>
              )}

              {(knowledge.data || []).map((k) => (
                <div key={k.id} className="rounded-lg border p-3">
                  <p className="text-sm font-semibold">{k.title}</p>
                  {k.key_terms.length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {k.key_terms.map((term) => (
                        <Badge key={term} variant="outline">
                          {term}
                        </Badge>
                      ))}
                    </div>
                  )}
                  <p className="mt-2 whitespace-pre-wrap text-sm leading-6">{k.summary}</p>
                  {k.chunk_ids.length > 0 && (
                    <p className="mt-1 text-xs text-muted-foreground">
                      依据 {k.chunk_ids.length} 个原文切片
                    </p>
                  )}
                </div>
              ))}
            </CardContent>
          </Card>

          <StructurePanel
            sourceId={id}
            kind={kind}
            onJumpToPage={jumpToPage}
            onSeek={seekVideo}
          />

          <UsagePanel sourceId={id} compact />

          <NotesPanel
            sourceId={id}
            notes={notes.data || []}
            loading={notes.isLoading}
            onGenerate={() => generateNotes.mutate()}
            generating={generateNotes.isPending}
            onChanged={() => queryClient.invalidateQueries({ queryKey: ["notes", id] })}
            onBanner={setBanner}
          />
        </section>
      </main>

      {confirmDelete && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-foreground/40 p-4 backdrop-blur-sm animate-fade-in"
          onClick={() => setConfirmDelete(false)}
        >
          <Card
            className="w-full max-w-md shadow-lift animate-scale-in"
            onClick={(e) => e.stopPropagation()}
          >
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                <Trash2 className="h-4 w-4 text-destructive" />
                确认删除？
              </CardTitle>
              <CardDescription>原文文件、AI 梳理、笔记、Quiz 记录都会被一并删除。</CardDescription>
            </CardHeader>
            <CardContent className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setConfirmDelete(false)} disabled={removeSource.isPending}>
                取消
              </Button>
              <Button variant="destructive" onClick={() => removeSource.mutate()} pending={removeSource.isPending}>
                {removeSource.isPending ? "删除中…" : "确认删除"}
              </Button>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}

function StatusBadge({ status }: { status?: string }) {
  if (!status) return <span>—</span>;
  const tone =
    status === "ready"
      ? { tone: "success" as const, dot: "bg-success" }
      : status === "failed"
        ? { tone: "destructive" as const, dot: "bg-destructive" }
        : { tone: "warning" as const, dot: "bg-warning animate-pulse-soft" };
  return (
    <Badge variant={tone.tone}>
      <span className={`status-dot ${tone.dot}`} />
      {status}
    </Badge>
  );
}

function NotesPanel({
  sourceId,
  notes,
  loading,
  onGenerate,
  generating,
  onChanged,
  onBanner,
}: {
  sourceId: string;
  notes: Note[];
  loading: boolean;
  onGenerate: () => void;
  generating: boolean;
  onChanged: () => void;
  onBanner: (b: { kind: "ok" | "err"; text: string }) => void;
}) {
  const [draftTitle, setDraftTitle] = useState("");
  const [draftBody, setDraftBody] = useState("");

  const create = useMutation({
    mutationFn: () =>
      api<Note>(`/api/v1/sources/${sourceId}/notes`, {
        method: "POST",
        body: JSON.stringify({ title: draftTitle || "未命名笔记", content: draftBody, origin: "manual" }),
      }),
    onSuccess: () => {
      setDraftTitle("");
      setDraftBody("");
      onChanged();
    },
    onError: (err) => onBanner({ kind: "err", text: err instanceof Error ? err.message : "保存笔记失败" }),
  });

  const update = useMutation({
    mutationFn: (payload: { id: string; title: string; content: string }) =>
      api<Note>(`/api/v1/notes/${payload.id}`, {
        method: "PATCH",
        body: JSON.stringify({ title: payload.title, content: payload.content }),
      }),
    onSuccess: () => {
      onChanged();
      onBanner({ kind: "ok", text: "笔记已更新" });
    },
    onError: (err) => onBanner({ kind: "err", text: err instanceof Error ? err.message : "更新失败" }),
  });

  const remove = useMutation({
    mutationFn: (noteId: string) => api<void>(`/api/v1/notes/${noteId}`, { method: "DELETE" }),
    onSuccess: () => onChanged(),
    onError: (err) => onBanner({ kind: "err", text: err instanceof Error ? err.message : "删除笔记失败" }),
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="text-base">我的学习笔记</CardTitle>
          <Button size="sm" variant="outline" onClick={onGenerate} disabled={generating}>
            {generating ? "生成中…" : "LLM 再生成"}
          </Button>
        </div>
        <CardDescription>
          手动记录或由 AI 起草。随「导出」一起保存到 JSON，可用「导入」还原。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2 rounded-lg border bg-muted/30 p-3">
          <Input
            placeholder="笔记标题"
            value={draftTitle}
            onChange={(e) => setDraftTitle(e.target.value)}
          />
          <Textarea
            placeholder="写下你的理解、疑问或联想…"
            value={draftBody}
            onChange={(e) => setDraftBody(e.target.value)}
          />
          <div className="flex justify-end">
            <Button size="sm" onClick={() => create.mutate()} disabled={create.isPending}>
              {create.isPending ? "保存中…" : "添加笔记"}
            </Button>
          </div>
        </div>

        {loading && <p className="text-sm text-muted-foreground">加载中…</p>}
        {!loading && notes.length === 0 && (
          <p className="text-sm text-muted-foreground">还没有笔记，从上面写一条吧。</p>
        )}
        {notes.map((note) => (
          <NoteRow
            key={note.id}
            note={note}
            saving={update.isPending}
            onSave={(title, content) => update.mutate({ id: note.id, title, content })}
            onDelete={() => remove.mutate(note.id)}
          />
        ))}
      </CardContent>
    </Card>
  );
}

function NoteRow({
  note,
  saving,
  onSave,
  onDelete,
}: {
  note: Note;
  saving: boolean;
  onSave: (title: string, content: string) => void;
  onDelete: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(note.title);
  const [content, setContent] = useState(note.content);

  useEffect(() => {
    setTitle(note.title);
    setContent(note.content);
  }, [note.title, note.content]);

  return (
    <div className="rounded-lg border p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          {editing ? (
            <Input value={title} onChange={(e) => setTitle(e.target.value)} />
          ) : (
            <p className="truncate text-sm font-semibold">{note.title}</p>
          )}
          <p className="mt-0.5 text-xs text-muted-foreground">
            {note.origin === "llm" ? "AI 草稿" : "手动"} · {formatTime(note.created_at)}
          </p>
        </div>
        <div className="flex shrink-0 gap-1">
          {editing ? (
            <>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  setEditing(false);
                  setTitle(note.title);
                  setContent(note.content);
                }}
              >
                取消
              </Button>
              <Button
                size="sm"
                disabled={saving}
                onClick={() => {
                  onSave(title, content);
                  setEditing(false);
                }}
              >
                保存
              </Button>
            </>
          ) : (
            <>
              <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>
                编辑
              </Button>
              <Button size="sm" variant="ghost" onClick={onDelete}>
                删除
              </Button>
            </>
          )}
        </div>
      </div>
      {editing ? (
        <Textarea className="mt-2" value={content} onChange={(e) => setContent(e.target.value)} />
      ) : (
        <p className="mt-2 whitespace-pre-wrap text-sm leading-6">{note.content}</p>
      )}
    </div>
  );
}
