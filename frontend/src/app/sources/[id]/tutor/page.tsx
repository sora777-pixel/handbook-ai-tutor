"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams, useRouter } from "next/navigation";
import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertCircle,
  Bot,
  Download,
  Loader2,
  MessageSquareQuote,
  Send,
  Sparkles,
  Upload,
  User,
} from "lucide-react";
import { AppHeader } from "@/components/app-header";
import { SourcePreview, type SourcePreviewHandle } from "@/components/source-preview";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { api, useAuth } from "@/lib/api";
import { authedUrl, downloadJson, pickJsonFile, slugify } from "@/lib/io";
import { CONTENT_TYPE_LABELS, type Citation, type ImportResult, type Source } from "@/lib/types";

type Msg = { id: string; role: string; content: string; citations: Citation[] };

function CitationCard({
  c,
  onJump,
  pagePrefix = "PDF p.",
}: {
  c: Citation;
  onJump?: (c: Citation) => void;
  /** EPUB sources address content by chapter, so their pages are "ch.3". */
  pagePrefix?: string;
}) {
  const typeLabel = CONTENT_TYPE_LABELS[c.content_type] ?? c.content_type;
  const canJump = c.start_time != null || c.page_number != null || c.printed_page != null;
  return (
    <button
      type="button"
      onClick={() => onJump?.(c)}
      disabled={!onJump || !canJump}
      className={[
        "w-full space-y-1.5 rounded-lg border border-border/60 bg-background/60 p-2.5 text-left text-xs",
        onJump && canJump
          ? "cursor-pointer transition-colors hover:border-primary/50 hover:bg-primary/[0.04]"
          : "",
      ].join(" ")}
    >
      <div className="flex flex-wrap items-center gap-1.5">
        {c.printed_page != null && <Badge>书内 p.{c.printed_page}</Badge>}
        {c.page_number != null && c.page_number !== c.printed_page && (
          <Badge variant="outline">{pagePrefix}{c.page_number}</Badge>
        )}
        {c.start_time != null && (
          <Badge variant="outline">
            {Math.floor(c.start_time / 60)}:{String(Math.floor(c.start_time % 60)).padStart(2, "0")}
          </Badge>
        )}
        {c.printed_page == null && c.page_number == null && c.start_time == null && (
          <Badge variant="outline">{c.locator || c.chunk_id.slice(0, 8)}</Badge>
        )}
        <Badge variant="soft">{typeLabel}</Badge>
        {canJump && onJump && <span className="text-primary">点击跳转原文</span>}
        {c.score != null && (
          <span className="text-muted-foreground">score {c.score.toFixed(2)}</span>
        )}
      </div>
      {c.section_title && (
        <p className="font-medium text-foreground/80">{c.section_title}</p>
      )}
      <div className="max-h-48 overflow-y-auto whitespace-pre-wrap pr-1 text-muted-foreground leading-relaxed">
        {c.quote}
      </div>
    </button>
  );
}

export default function TutorPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const token = useAuth((s) => s.token);
  const router = useRouter();
  const qc = useQueryClient();
  const [message, setMessage] = useState("这份资料的主要内容是什么？");
  const [ioBanner, setIoBanner] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const previewRef = useRef<SourcePreviewHandle>(null);

  useEffect(() => {
    if (!token) router.replace("/login");
  }, [token, router]);

  const source = useQuery({
    queryKey: ["source", id],
    queryFn: () => api<Source>(`/api/v1/sources/${id}`),
    enabled: !!token && !!id,
  });

  const history = useQuery({
    queryKey: ["tutor", id],
    queryFn: () => api<Msg[]>(`/api/v1/sources/${id}/tutor/messages`),
    enabled: !!token,
  });

  const fileUrl = useMemo(
    () => (id && token ? authedUrl(`/api/v1/sources/${id}/file`) : ""),
    [id, token]
  );

  const send = useMutation({
    mutationFn: (text: string) =>
      api<Msg>(`/api/v1/sources/${id}/tutor/chat`, {
        method: "POST",
        body: JSON.stringify({ message: text }),
      }),
    onSuccess: () => {
      setMessage("");
      void qc.invalidateQueries({ queryKey: ["tutor", id] });
    },
  });

  const exportChat = useMutation({
    mutationFn: () => api<Record<string, unknown>>(`/api/v1/sources/${id}/tutor/export`),
    onSuccess: (payload) => {
      downloadJson(`${slugify(source.data?.title || source.data?.filename, "tutor")}-qa.json`, payload);
      setIoBanner({ kind: "ok", text: "已导出本资料的问答记录" });
    },
    onError: (err) => setIoBanner({ kind: "err", text: err instanceof Error ? err.message : "导出失败" }),
  });

  const importChat = useMutation({
    mutationFn: async () => {
      const raw = (await pickJsonFile()) as Record<string, unknown>;
      const bundle =
        Array.isArray(raw.tutor) && !("notes" in raw)
          ? { ...raw, notes: [], knowledge: [], quizzes: [], records: [], summary: null }
          : raw;
      return api<ImportResult>(`/api/v1/sources/${id}/tutor/import`, {
        method: "POST",
        body: JSON.stringify({ bundle, mode: "merge" }),
      });
    },
    onSuccess: (res) => {
      setIoBanner({
        kind: "ok",
        text: `导入完成：新增 ${res.tutor_created ?? 0} 条对话，跳过重复 ${res.tutor_skipped ?? 0} 条`,
      });
      void qc.invalidateQueries({ queryKey: ["tutor", id] });
    },
    onError: (err) => setIoBanner({ kind: "err", text: err instanceof Error ? err.message : "导入失败" }),
  });

  const status = source.data?.status;
  const ready = status === "ready";
  const failed = status === "failed";
  const blockedReason = failed
    ? `资料解析失败${source.data?.error_message ? `：${source.data.error_message}` : ""}。请回到原文页检查配置后重新导入。`
    : status && status !== "ready"
      ? "资料仍在解析中，解析完成后即可提问。"
      : null;

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [history.data, send.isPending]);

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (ready && message.trim()) send.mutate(message.trim());
  }

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-background">
      <AppHeader />

      <div className="mx-auto grid w-full max-w-[1600px] min-h-0 flex-1 grid-cols-1 items-stretch gap-4 px-4 py-4 lg:grid-cols-2">
        <SourcePreview
          ref={previewRef}
          fileUrl={fileUrl}
          kind={source.data?.kind}
          filename={source.data?.filename}
          sourceId={id}
        />
      <div className="mx-auto flex w-full min-h-0 flex-1 flex-col">
        <div className="flex items-center justify-between gap-3 border-b py-4">
          <div className="flex items-center gap-3">
            <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-brand text-white shadow-soft">
              <MessageSquareQuote className="h-5 w-5" />
            </span>
            <div>
              <h1 className="text-xl font-semibold tracking-tight">Tutor Q&A</h1>
              <p className="text-xs text-muted-foreground">
                依据检索到的原文作答，并可用通用知识补充；出处只标注真正用到的页码或时间戳。
              </p>
            </div>
          </div>
          <div className="flex shrink-0 gap-2">
            <Button size="sm" variant="outline" onClick={() => exportChat.mutate()} pending={exportChat.isPending}>
              <Download />
              导出对话
            </Button>
            <Button size="sm" variant="outline" onClick={() => importChat.mutate()} pending={importChat.isPending}>
              <Upload />
              导入对话
            </Button>
          </div>
        </div>

        <div
          ref={scrollRef}
          className="scroll-smooth-x min-h-0 flex-1 space-y-4 overflow-y-auto py-4"
        >
          {history.isLoading && (
            <div className="space-y-3">
              <SkeletonBubble who="user" />
              <SkeletonBubble who="bot" />
            </div>
          )}
          {!history.isLoading && (history.data?.length ?? 0) === 0 && (
            <div className="rounded-xl border border-dashed bg-card/50 p-8 text-center">
              <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-xl bg-gradient-brand text-white shadow-soft">
                <Sparkles className="h-6 w-6" />
              </div>
              <p className="mt-3 text-sm font-medium">开始第一轮对话</p>
              <p className="mt-1 text-xs text-muted-foreground">
                导师会引用资料原文，也可以补充讲解；不会为补充知识伪造出处。
              </p>
            </div>
          )}
          {(history.data || []).map((m) => (
            <Bubble
              key={m.id}
              role={m.role}
              content={m.content}
              citations={m.citations}
              onJump={(c) => previewRef.current?.jumpToCitation(c)}
              pagePrefix={source.data?.kind === "epub" ? "ch." : "PDF p."}
            />
          ))}
          {send.isPending && (
            <Bubble role="assistant" content="" pending citations={[]} />
          )}
        </div>

        <form
          className="space-y-2 border-t bg-card/40 py-4 backdrop-blur-sm"
          onSubmit={onSubmit}
        >
          {ioBanner && (
            <div
              className={`flex items-start gap-2 rounded-lg border px-3 py-2 text-sm ${
                ioBanner.kind === "ok"
                  ? "border-success/30 bg-success/5"
                  : "border-destructive/30 bg-destructive/5 text-destructive"
              }`}
            >
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              <span className="leading-relaxed break-all">{ioBanner.text}</span>
            </div>
          )}
          {(blockedReason || send.isError) && (
            <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive animate-fade-in">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              <span className="leading-relaxed break-all">
                {send.isError
                  ? send.error instanceof Error
                    ? send.error.message
                    : "发送失败"
                  : blockedReason}
                {failed && (
                  <>
                    {" "}
                    <a className="underline underline-offset-4" href={`/sources/${id}`}>
                      返回原文并重试
                    </a>
                  </>
                )}
              </span>
            </div>
          )}
          <div className="flex items-end gap-2">
            <Textarea
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              rows={3}
              placeholder="把你最想问的问题写在这里…"
              className="flex-1"
              disabled={!ready}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  if (ready && message.trim()) send.mutate(message.trim());
                }
              }}
            />
            <Button
              type="submit"
              variant="gradient"
              size="lg"
              pending={send.isPending}
              disabled={!ready || !message.trim()}
              className="h-[88px] px-5"
            >
              <span className="flex flex-col items-center gap-1">
                {send.isPending ? <Loader2 className="animate-spin" /> : <Send />}
                <span className="text-[10px] opacity-80">Ctrl+↵</span>
              </span>
            </Button>
          </div>
        </form>
      </div>
      </div>
    </div>
  );
}

function Bubble({
  role,
  content,
  citations,
  pending = false,
  onJump,
  pagePrefix = "PDF p.",
}: {
  role: string;
  content: string;
  citations: Citation[];
  pending?: boolean;
  onJump?: (c: Citation) => void;
  /** EPUB sources address content by chapter, so their pages are "ch.3". */
  pagePrefix?: string;
}) {
  const isUser = role === "user";
  return (
    <div className={["flex gap-2.5", isUser ? "flex-row-reverse" : ""].join(" ")}>
      <span
        className={[
          "flex h-8 w-8 shrink-0 items-center justify-center rounded-full shadow-soft",
          isUser
            ? "bg-secondary text-secondary-foreground"
            : "bg-gradient-brand text-white",
        ].join(" ")}
      >
        {isUser ? <User className="h-4 w-4" /> : <Bot className="h-4 w-4" />}
      </span>
      <div
        className={[
          "max-w-[80%] space-y-2 rounded-2xl px-4 py-3 text-sm leading-relaxed",
          isUser
            ? "rounded-tr-sm bg-secondary text-secondary-foreground"
            : "rounded-tl-sm border bg-card shadow-soft",
          pending && "animate-pulse-soft",
        ].join(" ")}
      >
        {pending ? (
          <span className="inline-flex items-center gap-1 text-muted-foreground">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-primary [animation-delay:-0.3s]" />
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-primary [animation-delay:-0.15s]" />
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-primary" />
          </span>
        ) : (
          <>
            <p className="whitespace-pre-wrap break-words">{content}</p>
            {citations && citations.length > 0 && (
              <div className="mt-2 space-y-2 border-t pt-2">
                <p className="text-xs font-medium text-muted-foreground">引用来源</p>
                {citations.map((c) => (
                  <CitationCard
                    key={c.chunk_id}
                    c={c}
                    onJump={onJump}
                    pagePrefix={pagePrefix}
                  />
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function SkeletonBubble({ who }: { who: "user" | "bot" }) {
  return (
    <div className={["flex gap-2.5", who === "user" ? "flex-row-reverse" : ""].join(" ")}>
      <div className="skeleton h-8 w-8 rounded-full" />
      <div className="space-y-1.5">
        <div className="skeleton h-3 w-32" />
        <div className="skeleton h-3 w-48" />
      </div>
    </div>
  );
}
