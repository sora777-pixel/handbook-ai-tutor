"use client";

import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Clock, CornerDownRight, Layers, ScanLine } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";
import { emitReaderJump } from "@/lib/reader-bus";
import { CONTENT_TYPE_LABELS, type OutlineEntry, type SourceStructure } from "@/lib/types";

/** Colour per content type, so the split is readable at a glance. */
const TYPE_TONE: Record<string, string> = {
  body: "bg-slate-100 text-slate-700",
  heading: "bg-indigo-100 text-indigo-700",
  caption: "bg-amber-100 text-amber-800",
  formula: "bg-violet-100 text-violet-700",
  table: "bg-emerald-100 text-emerald-700",
  figure: "bg-rose-100 text-rose-700",
  reference: "bg-stone-100 text-stone-700",
  outline: "bg-sky-100 text-sky-700",
};

/** Seconds -> `m:ss` (or `h:mm:ss`), matching the player's own clock. */
function formatClock(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

function TypeChip({ kind, count }: { kind: string; count: number }) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium transition-transform hover:scale-105 ${
        TYPE_TONE[kind] ?? "bg-muted text-muted-foreground"
      }`}
    >
      {CONTENT_TYPE_LABELS[kind] ?? kind}
      <span className="tabular-nums opacity-70">{count}</span>
    </span>
  );
}

type TreeNode = { node: OutlineEntry; index: number; children: TreeNode[] };

/** Flat `level`-tagged list -> nested tree, so L2/L3 sit under their chapter. */
function buildTree(nodes: OutlineEntry[]): TreeNode[] {
  const roots: TreeNode[] = [];
  const stack: TreeNode[] = [];
  nodes.forEach((node, index) => {
    const item: TreeNode = { node, index, children: [] };
    const level = node.level || 1;
    while (stack.length > 0 && (stack[stack.length - 1].node.level || 1) >= level) stack.pop();
    if (stack.length > 0) stack[stack.length - 1].children.push(item);
    else roots.push(item);
    stack.push(item);
  });
  return roots;
}

function collectKeys(nodes: TreeNode[], into: Set<number>): Set<number> {
  for (const item of nodes) {
    into.add(item.index);
    collectKeys(item.children, into);
  }
  return into;
}

export function StructurePanel({
  sourceId,
  kind,
  onJumpToPage,
  onSeek,
}: {
  sourceId: string;
  /** Fallback when the structure payload predates the `kind` field. */
  kind?: string;
  /** PDF: scroll the embedded viewer to a physical page. */
  onJumpToPage?: (page: number) => void;
  /** Recording: move the player's playhead. */
  onSeek?: (seconds: number) => void;
}) {
  const [showChunks, setShowChunks] = useState(false);

  const structure = useQuery({
    queryKey: ["structure", sourceId],
    queryFn: () => api<SourceStructure>(`/api/v1/sources/${sourceId}/structure?limit=400`),
  });

  const data = structure.data;
  const outline = useMemo(() => data?.outline ?? [], [data]);
  const tree = useMemo(() => buildTree(outline), [outline]);
  const [collapsed, setCollapsed] = useState<Set<number>>(() => new Set());
  const allKeys = useMemo(() => collectKeys(tree, new Set<number>()), [tree]);

  if (structure.isLoading) {
    return (
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">文档结构解析</CardTitle>
          <CardDescription>读取中…</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="skeleton h-4 w-2/3" />
          <div className="skeleton h-4 w-1/2" />
          <div className="skeleton h-32 w-full" />
        </CardContent>
      </Card>
    );
  }
  if (!data) return null;

  const counts = data.content_types || {};
  const resolvedKind = data.kind || kind || "pdf";
  const isVideo = resolvedKind === "video";
  const isImage = resolvedKind === "image";
  const isEpub = resolvedKind === "epub";
  const chapters = tree.length;
  let visible = 0;
  const walk = (nodes: TreeNode[]) => {
    for (const item of nodes) {
      visible += 1;
      if (!collapsed.has(item.index)) walk(item.children);
    }
  };
  walk(tree);

  const toggle = (index: number) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });

  const renderNodes = (nodes: TreeNode[]) =>
    nodes.map((item) => {
      const { node, index, children } = item;
      const hasChildren = children.length > 0;
      const open = hasChildren && !collapsed.has(index);
      const startTime = node.start_time ?? null;
      const printable =
        node.printed_page ?? (node.page_number ? node.page_number : null);
      const jumpable = isVideo
        ? Boolean(onSeek) && startTime != null
        : isEpub
          ? node.page_number != null // an EPUB chapter is addressed by its ordinal
          : !isImage && Boolean(onJumpToPage) && printable != null;

      const activate = () => {
        if (isVideo) {
          if (onSeek && startTime != null) onSeek(startTime);
          return;
        }
        if (isEpub) {
          // The reader owns chapter switching (and the TOC highlight), so the
          // request goes through the bus rather than through a page jump: the
          // click must move the book *and* the highlighted row on the left.
          emitReaderJump({
            chapter: (node.page_number ?? 1) - 1,
            title: node.title,
            from: "structure",
          });
          return;
        }
        if (onJumpToPage && printable != null) onJumpToPage(printable);
      };

      return (
        <div key={index}>
          <div
            className="flex items-center gap-1.5 rounded px-1.5 py-1 transition-colors hover:bg-background"
            style={{ paddingLeft: `${(node.level - 1) * 16 + 6}px` }}
          >
            {hasChildren ? (
              <button
                type="button"
                onClick={() => toggle(index)}
                aria-expanded={open}
                aria-label={open ? "收起子章节" : "展开子章节"}
                className="flex h-4 w-4 shrink-0 items-center justify-center rounded text-primary hover:bg-primary/10"
              >
                {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
              </button>
            ) : (
              <span
                className={`flex h-4 w-4 shrink-0 items-center justify-center ${
                  node.level === 1 ? "text-primary" : "text-muted-foreground/50"
                }`}
              >
                {node.level === 1 ? (
                  <ChevronDown className="h-3 w-3" />
                ) : (
                  <CornerDownRight className="h-3 w-3" />
                )}
              </span>
            )}

            <span className="shrink-0 rounded bg-muted px-1.5 text-[10px] text-muted-foreground">
              L{node.level}
            </span>

            <button
              type="button"
              onClick={activate}
              disabled={!jumpable}
              title={
                jumpable
                  ? isVideo
                    ? `跳转到 ${formatClock(startTime)}`
                    : isEpub
                      ? `跳到「${node.title}」并在目录中定位`
                      : `跳转到 PDF 第 ${printable} 页`
                  : "该条目没有可跳转的位置"
              }
              className={`min-w-0 truncate rounded px-1 text-left ${
                jumpable
                  ? "text-foreground/90 underline-offset-4 hover:text-primary hover:underline"
                  : "text-foreground/70"
              }`}
            >
              {node.number ? `${node.number} ` : ""}
              {node.title || "（无标题）"}
            </button>

            <span className="ml-auto shrink-0 tabular-nums text-muted-foreground">
              {isVideo ? (
                <>
                  {formatClock(startTime)}
                  {node.end_time != null && startTime != null
                    ? `–${formatClock(node.end_time)}`
                    : ""}
                </>
              ) : (
                <>
                  书内 p.{node.printed_page ?? "?"}
                  {node.page_end != null && node.page_number != null
                    ? `–${(node.printed_page ?? 0) + (node.page_end - node.page_number)}`
                    : ""}
                </>
              )}
            </span>
          </div>
          {open && <div className="border-l border-dashed border-border/70">{renderNodes(children)}</div>}
        </div>
      );
    });

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center gap-2">
          <span className="flex h-7 w-7 items-center justify-center rounded-md bg-primary/10 text-primary">
            <Layers className="h-3.5 w-3.5" />
          </span>
          <div>
            <CardTitle className="text-base">文档结构解析</CardTitle>
            <CardDescription>
              {isVideo
                ? "语音转写被切分成带时间戳的片段，再归并成章节。点击章节可直接把播放进度跳到该处。"
                : "正文、标题、图注、公式、表格、图内文字被分别识别并标注归属。页码同时给出「书内页码」与「PDF 页码」，两者相差 " +
                  (data.page_offset != null ? Math.abs(data.page_offset) : "?") +
                  " 页（前置页所致）。点击条目标题可跳转到对应页。"}
            </CardDescription>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          {isVideo ? (
            <>
              <Badge variant="outline">
                <Clock className="h-3 w-3" />
                时长 {formatClock(data.duration)}
              </Badge>
              <Badge variant="outline">章节 {chapters}</Badge>
            </>
          ) : (
            <>
              <Badge variant="outline">
                <ScanLine className="h-3 w-3" />
                PDF 共 {data.page_count ?? "?"} 页
              </Badge>
              {data.page_offset != null && (
                <Badge variant="outline">
                  书内页码 = PDF 页码 {data.page_offset >= 0 ? "+" : "−"}
                  {Math.abs(data.page_offset)}
                </Badge>
              )}
              <Badge variant="outline">章节 {chapters}</Badge>
            </>
          )}
          <Badge variant="outline">切块 {data.chunk_total}</Badge>
          {chapters > 0 && visible < allKeys.size && (
            <button
              type="button"
              className="text-xs text-primary underline-offset-4 hover:underline"
              onClick={() => setCollapsed(new Set())}
            >
              展开全部
            </button>
          )}
          {chapters > 0 && visible === allKeys.size && allKeys.size > 1 && (
            <button
              type="button"
              className="text-xs text-muted-foreground underline-offset-4 hover:underline"
              onClick={() => setCollapsed(new Set(allKeys))}
            >
              收起全部
            </button>
          )}
        </div>

        <div className="flex flex-wrap gap-1.5">
          {Object.entries(counts)
            .sort((a, b) => b[1] - a[1])
            .map(([type, count]) => (
              <TypeChip key={type} kind={type} count={count} />
            ))}
        </div>

        {data.outline.length > 0 ? (
          <div>
            <p className="mb-1 text-xs font-medium text-muted-foreground">
              {isVideo ? "章节（时间轴 · 标题 · 起止时间）" : "章节树（层级 · 标题 · 书内页码）"}
            </p>
            <div className="max-h-72 scroll-smooth-x overflow-y-auto rounded-lg border bg-muted/20 p-3 font-mono text-xs leading-6">
              {renderNodes(tree)}
            </div>
          </div>
        ) : isVideo ? (
          <p className="text-xs text-muted-foreground">
            该录像还没有可用的章节结构：语音转写可能尚未完成或未识别出可用语音。
            流水线跑完后这里会按时间轴列出章节，点击即可跳转播放位置。
          </p>
        ) : (
          <p className="text-xs text-muted-foreground">
            未识别出章节结构（该文档可能没有标题层级或目录页）。
          </p>
        )}

        <div>
          <Button variant="outline" size="sm" onClick={() => setShowChunks((v) => !v)}>
            {showChunks ? (
              <>
                <ChevronDown /> 隐藏切片明细
              </>
            ) : (
              <>
                <ChevronRight /> 查看切片明细（前 {data.chunks.length} 条）
              </>
            )}
          </Button>
        </div>

        {showChunks && (
          <div className="max-h-96 scroll-smooth-x space-y-2 overflow-y-auto pr-1">
            {data.chunks.map((chunk) => (
              <div
                key={chunk.id}
                className="rounded-lg border bg-card/60 p-2.5 text-xs transition-colors hover:bg-card"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span
                    className={`rounded-full px-2 py-0.5 font-medium ${
                      TYPE_TONE[chunk.content_type] ?? "bg-muted"
                    }`}
                  >
                    {CONTENT_TYPE_LABELS[chunk.content_type] ?? chunk.content_type}
                  </span>
                  {chunk.start_time != null && (
                    <button
                      type="button"
                      onClick={() => onSeek?.(chunk.start_time as number)}
                      disabled={!onSeek}
                      className={
                        onSeek
                          ? "rounded-full bg-primary/10 px-2 py-0.5 tabular-nums text-primary underline-offset-4 hover:underline"
                          : "rounded-full bg-muted px-2 py-0.5 tabular-nums"
                      }
                    >
                      {formatClock(chunk.start_time)}
                      {chunk.end_time != null ? `–${formatClock(chunk.end_time)}` : ""}
                    </button>
                  )}
                  {chunk.printed_page != null && <Badge>书内 p.{chunk.printed_page}</Badge>}
                  {chunk.page_number != null && chunk.page_number !== chunk.printed_page && (
                    <Badge variant="outline">PDF p.{chunk.page_number}</Badge>
                  )}
                  {chunk.heading_level != null && (
                    <span className="text-muted-foreground">L{chunk.heading_level}</span>
                  )}
                </div>
                {chunk.section_title && (
                  <p className="mt-1 text-foreground/80">{chunk.section_title}</p>
                )}
                <p className="mt-1 whitespace-pre-wrap text-muted-foreground">{chunk.preview}</p>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
