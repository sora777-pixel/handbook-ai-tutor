"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, List, Loader2, Minus, Plus } from "lucide-react";
import { api } from "@/lib/api";
import { authedUrl } from "@/lib/io";
import { subscribeReaderJump, type ReaderTarget } from "@/lib/reader-bus";

type Chapter = { index: number; title: string; chars: number };
type Contents = { title: string; authors: string[]; chapters: Chapter[]; cover?: boolean };
type Fragment = { index: number; total: number; title: string; html: string; next: number | null };

/**
 * In-app EPUB reader — rendered inline, the way the PDF viewer works.
 *
 * The first version put each chapter in an <iframe>. Two things went wrong with
 * it in practice: while the backend was busy the frame showed the browser's own
 * "localhost 拒绝了我们的连接请求", and switching chapters re-created the frame so
 * the page could stay blank. There was also a download button that popped a save
 * dialog on a device where the user only wanted to read.
 *
 * So: no iframe, no download. The backend hands over a sanitised fragment
 * (`?format=json`, see backend/app/api/sources_epub.py), this component renders
 * it into a scrolling column, and the next chapter is appended when the reader
 * reaches the bottom — a continuous book, like a PDF, with a table of contents
 * for jumping and page-by-page keys for deliberate paging.
 */
const CHAPTER_WIDTH = 760;

export function EpubReader({
  sourceId,
  onError,
}: {
  sourceId: string;
  onError?: (message: string) => void;
}) {
  const [contents, setContents] = useState<Contents | null>(null);
  const [fragments, setFragments] = useState<Fragment[]>([]);
  const [loadingMore, setLoadingMore] = useState(false);
  const [current, setCurrent] = useState(0);
  const [showToc, setShowToc] = useState(true);
  const [scale, setScale] = useState(1);
  const [loadError, setLoadError] = useState<string | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const requested = useRef<Set<number>>(new Set());

  // ---- table of contents -------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    api<Contents>(`/api/v1/sources/${sourceId}/epub`)
      .then((data) => {
        if (cancelled) return;
        setContents(data);
        setLoadError(null);
      })
      .catch((err) => {
        if (!cancelled) {
          const message = err instanceof Error ? err.message : "无法读取电子书目录";
          setLoadError(message);
          onError?.(message);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [sourceId, onError]);

  // ---- chapter loading (lazy, ordered) -----------------------------------
  const loadChapter = useCallback(
    async (index: number) => {
      if (requested.current.has(index)) return;
      requested.current.add(index);
      setLoadingMore(true);
      try {
        const fragment = await api<Fragment>(
          `/api/v1/sources/${sourceId}/epub/chapter?i=${index}&format=json`,
        );
        setFragments((prev) => {
          const rest = prev.filter((f) => f.index !== fragment.index);
          return [...rest, fragment].sort((a, b) => a.index - b.index);
        });
        setLoadError(null);
      } catch (err) {
        requested.current.delete(index); // allow a retry
        const message = err instanceof Error ? err.message : "章节加载失败";
        setLoadError(message);
        onError?.(message);
      } finally {
        setLoadingMore(false);
      }
    },
    [sourceId, onError],
  );

  useEffect(() => {
    setFragments([]);
    requested.current = new Set();
    setCurrent(0);
    void loadChapter(0);
  }, [loadChapter]);

  const total = contents?.chapters.length ?? 0;

  // ---- infinite scroll: append the next chapter near the bottom ----------
  const onScroll = useCallback(() => {
    const el = scroller.current;
    if (!el || loadingMore) return;

    const marks = Array.from(el.querySelectorAll<HTMLElement>("[data-chapter-index]"));
    let visible = current;
    for (const mark of marks) {
      if (mark.offsetTop - el.scrollTop <= 80) visible = Number(mark.dataset.chapterIndex);
    }
    if (visible !== current) setCurrent(visible);

    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 700;
    const lastLoaded = fragments.length ? fragments[fragments.length - 1].index : -1;
    if (nearBottom && lastLoaded + 1 < total) void loadChapter(lastLoaded + 1);
  }, [current, fragments, loadChapter, loadingMore, total]);

  const scrollToChapter = useCallback((index: number) => {
    const el = scroller.current;
    const target = el?.querySelector<HTMLElement>(`[data-chapter-index="${index}"]`);
    if (el && target) {
      el.scrollTo({ top: target.offsetTop - 12, behavior: "smooth" });
      setCurrent(index);
      return true;
    }
    return false;
  }, []);

  const jumpTo = useCallback(
    (index: number) => {
      if (index < 0 || (total > 0 && index >= total)) return;
      setShowToc(false);
      if (scrollToChapter(index)) return;
      void loadChapter(index).then(() => {
        requestAnimationFrame(() => scrollToChapter(index));
      });
    },
    [loadChapter, scrollToChapter, total],
  );

  // "翻页": one viewport at a time, like a PDF reader's page keys.
  const page = useCallback((direction: 1 | -1) => {
    const el = scroller.current;
    if (!el) return;
    el.scrollBy({ top: Math.max(240, el.clientHeight * 0.9) * direction, behavior: "smooth" });
  }, []);

  // Where a jump request lands. A chapter index is exact; a section title is what
  // a citation knows, so fall back to matching it against the table of contents.
  const resolveTarget = useCallback(
    (target: ReaderTarget): number | null => {
      if (typeof target.chapter === "number" && Number.isFinite(target.chapter)) {
        return Math.max(0, Math.min(target.chapter, Math.max(0, total - 1)));
      }
      const wanted = (target.title || "").trim();
      if (!wanted || !contents) return null;
      const wantedLower = wanted.toLowerCase();
      const exact = contents.chapters.find((c) => c.title.trim().toLowerCase() === wantedLower);
      if (exact) return exact.index;
      const partial = contents.chapters.find(
        (c) =>
          c.title.toLowerCase().includes(wantedLower) ||
          wantedLower.includes(c.title.trim().toLowerCase()),
      );
      return partial ? partial.index : null;
    },
    [contents, total],
  );

  useEffect(
    () =>
      subscribeReaderJump((target) => {
        const index = resolveTarget(target);
        if (index != null) jumpTo(index);
      }),
    [resolveTarget, jumpTo],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") return;
      if (e.key === "PageDown" || e.key === " ") {
        e.preventDefault();
        page(1);
      } else if (e.key === "PageUp") {
        e.preventDefault();
        page(-1);
      } else if (e.key === "ArrowRight") {
        jumpTo(current + 1);
      } else if (e.key === "ArrowLeft") {
        jumpTo(current - 1);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [page, jumpTo, current]);

  // In-book links arrive as #epub-chapter-N: switch chapters in place instead
  // of navigating the tab away from the reader.
  const onClick = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      const anchor = (e.target as HTMLElement | null)?.closest?.("a");
      if (!anchor) return;
      e.preventDefault();
      const href = anchor.getAttribute("href") || "";
      const match = href.match(/^#epub-chapter-(\d+)$/);
      if (match) jumpTo(Number(match[1]));
    },
    [jumpTo],
  );

  const fontStyle = useMemo(
    () => ({ fontSize: `${(scale * 17).toFixed(1)}px`, maxWidth: CHAPTER_WIDTH }),
    [scale],
  );

  if (loadError && !fragments.length) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 p-8 text-center">
        <p className="text-sm font-medium text-destructive">电子书暂时打不开</p>
        <p className="max-w-md text-xs text-muted-foreground">{loadError}</p>
        <p className="max-w-md text-xs text-muted-foreground">
          若提示连接失败，说明应用已退出：双击项目根目录的 start.bat 重新启动即可。
        </p>
      </div>
    );
  }

  if (!contents) {
    return (
      <div className="flex h-full items-center justify-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        正在打开这本书…
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0">
      {showToc && (
        <aside className="flex w-56 shrink-0 flex-col border-r bg-card/40">
          <div className="border-b px-3 py-2 text-xs font-medium text-muted-foreground">
            目录 · {total} 章
          </div>
          <nav className="min-h-0 flex-1 overflow-auto py-1">
            {contents.cover && (
              <button
                type="button"
                onClick={() => {
                  const el = scroller.current;
                  if (el) el.scrollTo({ top: 0, behavior: "smooth" });
                  setCurrent(-1);
                  setShowToc(false);
                }}
                className={[
                  "block w-full truncate px-3 py-2 text-left text-sm hover:bg-primary/5",
                  current === -1 ? "bg-primary/10 font-medium text-primary" : "text-foreground/80",
                ].join(" ")}
              >
                封面
              </button>
            )}
            {contents.chapters.map((c) => (
              <button
                key={c.index}
                type="button"
                onClick={() => jumpTo(c.index)}
                className={[
                  "block w-full truncate px-3 py-2 text-left text-sm hover:bg-primary/5",
                  c.index === current
                    ? "bg-primary/10 font-medium text-primary"
                    : "text-foreground/80",
                ].join(" ")}
                title={c.title}
              >
                {c.index + 1}. {c.title}
              </button>
            ))}
          </nav>
        </aside>
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex shrink-0 items-center gap-1 border-b bg-card/40 px-2 py-1.5 text-xs">
          <Tool active={showToc} onClick={() => setShowToc((v) => !v)} title="目录">
            <List className="h-4 w-4" />
          </Tool>
          <Tool onClick={() => jumpTo(current - 1)} disabled={current <= 0} title="上一章">
            <ChevronLeft className="h-4 w-4" />
          </Tool>
          <span className="px-1 text-muted-foreground">
            {current < 0 ? `封面 / 共 ${total} 章` : `第 ${current + 1} / ${total || "…"} 章`}
          </span>
          <Tool
            onClick={() => jumpTo(current + 1)}
            disabled={total > 0 && current >= total - 1}
            title="下一章"
          >
            <ChevronRight className="h-4 w-4" />
          </Tool>
          <span className="mx-2 h-4 w-px bg-border" />
          <Tool onClick={() => page(-1)} title="上一页（PageUp）">
            <ChevronLeft className="h-3.5 w-3.5" />
            <span className="text-[11px]">页</span>
          </Tool>
          <Tool onClick={() => page(1)} title="下一页（PageDown / 空格）">
            <span className="text-[11px]">页</span>
            <ChevronRight className="h-3.5 w-3.5" />
          </Tool>
          <span className="mx-2 h-4 w-px bg-border" />
          <Tool
            onClick={() => setScale((v) => Math.max(0.8, Number((v - 0.1).toFixed(2))))}
            title="缩小"
          >
            <Minus className="h-3.5 w-3.5" />
          </Tool>
          <span className="min-w-10 text-center text-muted-foreground">
            {Math.round(scale * 100)}%
          </span>
          <Tool
            onClick={() => setScale((v) => Math.min(1.6, Number((v + 0.1).toFixed(2))))}
            title="放大"
          >
            <Plus className="h-3.5 w-3.5" />
          </Tool>
          <span
            className="ml-auto max-w-[40%] truncate px-2 text-muted-foreground"
            title={contents.title}
          >
            {contents.title}
          </span>
        </div>

        <div
          ref={scroller}
          onScroll={onScroll}
          className="epub-scroll min-h-0 flex-1 overflow-auto bg-[#fffdf8] px-6 py-6"
        >
          <article onClick={onClick} className="epub-body mx-auto" style={fontStyle}>
            {contents.cover && (
              <section
                data-chapter-index={-1}
                className="epub-cover mb-10 border-b border-dashed border-[#e8e2d4] pb-8"
              >
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={authedUrl(`/api/v1/sources/${sourceId}/epub/cover`)}
                  alt={contents.title || "书籍封面"}
                  className="mx-auto max-h-[70vh] w-auto rounded-lg shadow-md"
                  onError={(e) => {
                    // No cover, or it failed to load: drop the block instead of
                    // leaving a broken-image icon at the top of the book.
                    const section = e.currentTarget.closest(".epub-cover");
                    if (section instanceof HTMLElement) section.style.display = "none";
                  }}
                />
                {contents.title && (
                  <h1 className="mt-5 text-center text-xl font-semibold">{contents.title}</h1>
                )}
                {contents.authors.length > 0 && (
                  <p className="mt-1 text-center text-sm text-muted-foreground">
                    {contents.authors.join("、")}
                  </p>
                )}
              </section>
            )}
            {fragments.map((fragment) => (
              <section
                key={fragment.index}
                data-chapter-index={fragment.index}
                className="epub-chapter border-b border-dashed border-[#e8e2d4] pb-8 pt-2 last:border-b-0"
                dangerouslySetInnerHTML={{ __html: fragment.html }}
              />
            ))}
            {loadingMore && (
              <p className="flex items-center justify-center gap-2 py-6 text-xs text-muted-foreground">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                正在载入下一章…
              </p>
            )}
            {!loadingMore && total > 0 && fragments.length >= total && (
              <p className="py-6 text-center text-xs text-muted-foreground">— 全书完 —</p>
            )}
          </article>
        </div>
      </div>
    </div>
  );
}

function Tool({
  children,
  onClick,
  disabled,
  title,
  active,
}: {
  children: React.ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  title?: string;
  active?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={[
        "inline-flex items-center gap-0.5 rounded-md px-2 py-1 transition-colors",
        "disabled:cursor-not-allowed disabled:opacity-35",
        active ? "bg-primary/15 text-primary" : "hover:bg-primary/10",
      ].join(" ")}
    >
      {children}
    </button>
  );
}