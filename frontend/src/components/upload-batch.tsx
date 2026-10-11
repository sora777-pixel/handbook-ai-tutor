"use client";

import { useCallback, useRef, useState } from "react";
import { AlertCircle, CheckCircle2, Loader2, UploadCloud } from "lucide-react";
import { api } from "@/lib/api";
import { Progress } from "@/components/ui/progress";

type Row = {
  id: string;
  name: string;
  size: number;
  state: "queued" | "uploading" | "accepted" | "failed";
  detail?: string;
  sourceId?: string;
};

/** How many uploads are in flight at once. The backend also caps concurrent
 *  parsing (INGEST_CONCURRENCY); this keeps the browser from opening ten
 *  simultaneous connections on a cheap laptop. */
const WIDTH = 3;

export type UploadOutcome = { ok: number; failed: number; lastSourceId?: string };

/**
 * Fire N files at the upload endpoint at once.
 *
 * One POST per file (the endpoint is per-file by design), a small worker pool
 * so the browser is not the bottleneck, and a per-file verdict: a rejected file
 * reports its own error and never cancels the others. The caller decides what to
 * do with the summary -- nothing is navigated automatically, because with a
 * batch there is no single "the" source to open.
 */
export function uploadMany(
  files: File[],
  onRow: (row: Row) => void,
  width = WIDTH,
): Promise<UploadOutcome> {
  let cursor = 0;
  let ok = 0;
  let failed = 0;
  let lastSourceId: string | undefined;

  const rows: Row[] = files.map((f, i) => ({
    id: `${i}-${f.name}-${f.size}`,
    name: f.name,
    size: f.size,
    state: "queued",
  }));
  rows.forEach(onRow);

  async function worker() {
    for (;;) {
      const index = cursor++;
      if (index >= rows.length) return;
      const row = rows[index];
      onRow({ ...row, state: "uploading" });
      try {
        const body = new FormData();
        body.append("file", files[index]);
        const created = await api<{ id: string; task_id?: string }>(
          "/api/v1/sources/upload",
          { method: "POST", body },
        );
        ok += 1;
        lastSourceId = created.id;
        onRow({ ...row, state: "accepted", sourceId: created.id });
      } catch (err) {
        failed += 1;
        onRow({
          ...row,
          state: "failed",
          detail: err instanceof Error ? err.message : "上传失败",
        });
      }
    }
  }

  return Promise.all(
    Array.from({ length: Math.min(width, Math.max(1, files.length)) }, worker),
  ).then(() => ({ ok, failed, lastSourceId }));
}

export function UploadDropzone({
  onFiles,
  busy,
}: {
  onFiles: (files: File[]) => void;
  busy: boolean;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);

  const pick = useCallback(
    (list: FileList | null) => {
      const files = Array.from(list ?? []);
      if (files.length) onFiles(files);
    },
    [onFiles],
  );

  return (
    <label
      onDragOver={(e) => {
        e.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragOver(false);
        pick(e.dataTransfer.files);
      }}
      className={[
        "group relative flex cursor-pointer flex-col items-center justify-center gap-3 rounded-xl border-2 border-dashed px-6 py-10 text-center transition-all duration-200",
        dragOver
          ? "border-primary bg-primary/5 scale-[1.01]"
          : "border-border bg-muted/30 hover:border-primary/50 hover:bg-primary/[0.03]",
        busy && "pointer-events-none opacity-60",
      ].join(" ")}
    >
      <input
        ref={inputRef}
        type="file"
        multiple
        accept=".pdf,.mp4,.epub,.png,.jpg,.jpeg,application/pdf,video/mp4,application/epub+zip,image/png,image/jpeg"
        disabled={busy}
        className="sr-only"
        onChange={(e) => {
          pick(e.target.files);
          if (inputRef.current) inputRef.current.value = "";
        }}
      />
      <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-gradient-brand text-white shadow-glow transition-transform group-hover:scale-110">
        {busy ? (
          <Loader2 className="h-7 w-7 animate-spin" />
        ) : (
          <UploadCloud className="h-7 w-7" />
        )}
      </div>
      <div className="space-y-1">
        <p className="text-base font-medium">
          {busy ? "正在上传…" : "点击或拖拽文件到这里（可多选）"}
        </p>
        <p className="text-xs text-muted-foreground">
          支持 PDF 课件、MP4 课程录像、EPUB 电子书/教材、扫描版教材截图；多个文件会并行解析
        </p>
      </div>
      {!busy && (
        <span className="rounded-full bg-primary/10 px-3 py-1 text-xs font-medium text-primary">
          选择文件
        </span>
      )}
    </label>
  );
}

export function UploadQueue({ rows }: { rows: Row[] }) {
  if (!rows.length) return null;
  const done = rows.filter((r) => r.state === "accepted" || r.state === "failed").length;
  return (
    <div className="space-y-2 rounded-xl border bg-card/40 p-3">
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>
          {done} / {rows.length} 已处理
        </span>
        <span>并发达 {WIDTH} 个/路</span>
      </div>
      <Progress value={Math.round((done / rows.length) * 100)} className="h-1.5" />
      <ul className="max-h-56 space-y-1 overflow-auto text-xs">
        {rows.map((r) => (
          <li key={r.id} className="flex items-center gap-2">
            {r.state === "uploading" || r.state === "queued" ? (
              <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-info" />
            ) : r.state === "accepted" ? (
              <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-success" />
            ) : (
              <AlertCircle className="h-3.5 w-3.5 shrink-0 text-destructive" />
            )}
            <span className="min-w-0 flex-1 truncate text-foreground/85">{r.name}</span>
            {r.detail && (
              <span className="max-w-[45%] truncate text-destructive" title={r.detail}>
                {r.detail}
              </span>
            )}
            {r.state === "accepted" && (
              <span className="text-muted-foreground">已进入解析队列</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

export type { Row as UploadRow };
