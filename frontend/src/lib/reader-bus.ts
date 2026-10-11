"use client";

/**
 * One tiny event bus for "take the reader to this place".
 *
 * Three different panels need to move the preview: the structure tree (点击章节),
 * a citation chip under a Tutor answer, and (future) a quiz explanation. Each
 * of them lives in a sibling component, so passing a callback down through the
 * page for every case would couple the page to every panel. A window event
 * keeps the panels ignorant of each other: whoever owns the document listens,
 * whoever wants to navigate emits.
 *
 * Targets are deliberately forgiving -- a caller may know a chapter index, a
 * physical page, a printed page, or only the section title (which is all a
 * citation knows for an EPUB, where "page" is a chapter ordinal).
 */
export type ReaderTarget = {
  /** EPUB chapter index (0-based, spine order). */
  chapter?: number | null;
  /** PDF page number (1-based, the viewer's own numbering). */
  page?: number | null;
  /** Section title, used when no index is known. */
  title?: string | null;
  /** Where the request came from, for logging/debugging in the console. */
  from?: string;
};

const EVENT = "tutor:reader-jump";

export function emitReaderJump(target: ReaderTarget): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent<ReaderTarget>(EVENT, { detail: target }));
}

export function subscribeReaderJump(handler: (target: ReaderTarget) => void): () => void {
  if (typeof window === "undefined") return () => {};
  const listener = (event: Event) => handler((event as CustomEvent<ReaderTarget>).detail);
  window.addEventListener(EVENT, listener);
  return () => window.removeEventListener(EVENT, listener);
}
