"use client";

import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import {
  ArrowLeft,
  CheckCircle2,
  Download,
  Eye,
  FileJson,
  History,
  Layers,
  ListChecks,
  Loader2,
  Send,
  Sparkles,
  Trash2,
  Upload,
  XCircle,
} from "lucide-react";
import { AppHeader } from "@/components/app-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { api, useAuth } from "@/lib/api";
import { QUESTION_TYPE_LABEL, downloadJson, formatTime, pickJsonFile, slugify } from "@/lib/io";
import type {
  Attempt,
  ImportResult,
  QuestionResult,
  Quiz,
  QuizDeleteOut,
  QuizList,
  QuizQuestion,
  QuizRecord,
  QuizSummary,
  Source,
  SourceStructure,
} from "@/lib/types";

type Draft = { selected_index?: number; text_answer?: string };
type RecordList = { source_id: string; records: QuizRecord[] };

export default function QuizPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const token = useAuth((s) => s.token);
  const router = useRouter();
  const queryClient = useQueryClient();
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [result, setResult] = useState<Attempt | null>(null);
  const [viewing, setViewing] = useState<Attempt | null>(null);
  const [banner, setBanner] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [selectedQuizId, setSelectedQuizId] = useState<string | null>(null);
  const [retryIds, setRetryIds] = useState<string[] | null>(null);
  // Which generated quiz set the confirm dialog is about, and whether the
  // user also wants its submission records gone.
  const [deleteTarget, setDeleteTarget] = useState<QuizSummary | null>(null);
  const [deleteWithAttempts, setDeleteWithAttempts] = useState(false);

  useEffect(() => {
    if (!token) router.replace("/login");
  }, [token, router]);

  const source = useQuery({
    queryKey: ["source", id],
    queryFn: () => api<Source>(`/api/v1/sources/${id}`),
    enabled: !!token && !!id,
    retry: false,
  });

  const quizList = useQuery({
    queryKey: ["quizzes", id],
    queryFn: () => api<QuizList>(`/api/v1/sources/${id}/quizzes`),
    enabled: !!token && !!id,
  });

  const quizQuery = useQuery({
    queryKey: ["quiz", id, selectedQuizId],
    queryFn: () =>
      api<Quiz>(
        selectedQuizId
          ? `/api/v1/sources/${id}/quiz?quiz_id=${selectedQuizId}`
          : `/api/v1/sources/${id}/quiz`
      ),
    enabled: !!token && !!id,
    retry: false,
  });

  const records = useQuery({
    queryKey: ["quiz-records", id],
    queryFn: () => api<RecordList>(`/api/v1/sources/${id}/quiz/records`),
    enabled: !!token && !!id,
  });

  const structure = useQuery({
    queryKey: ["structure", id],
    queryFn: () => api<SourceStructure>(`/api/v1/sources/${id}/structure`),
    enabled: !!token && !!id,
  });

  const generate = useMutation({
    mutationFn: () => api<Quiz>(`/api/v1/sources/${id}/quiz/generate`, { method: "POST" }),
    onSuccess: (quiz) => {
      setResult(null);
      setViewing(null);
      setDrafts({});
      setRetryIds(null);
      setSelectedQuizId(quiz.id);
      void queryClient.invalidateQueries({ queryKey: ["quizzes", id] });
      void queryClient.invalidateQueries({ queryKey: ["quiz", id] });
      setBanner({ kind: "ok", text: "已生成新一套练习题（此前的题目与作答记录仍保留）" });
    },
    onError: (err) => setBanner({ kind: "err", text: err instanceof Error ? err.message : "生成失败" }),
  });

  const submit = useMutation({
    mutationFn: (quizId: string) =>
      api<Attempt>(`/api/v1/quizzes/${quizId}/attempt`, {
        method: "POST",
        body: JSON.stringify({
          question_ids: retryIds,
          answers: Object.entries(drafts)
            .filter(([question_id]) => !retryIds || retryIds.includes(question_id))
            .map(([question_id, d]) => ({
              question_id,
              selected_index: d.selected_index ?? null,
              text_answer: d.text_answer ?? null,
            })),
        }),
      }),
    onSuccess: (attempt) => {
      setResult(attempt);
      setViewing(null);
      setRetryIds(null);
      void queryClient.invalidateQueries({ queryKey: ["quiz-records", id] });
      setBanner({
        kind: "ok",
        text: `已提交，${attempt.graded_count} 道题生成了 AI 解析`,
      });
    },
    onError: (err) => setBanner({ kind: "err", text: err instanceof Error ? err.message : "提交失败" }),
  });

  const quiz = quizQuery.data || generate.data;
  const visibleQuestions = useMemo(() => {
    if (!quiz) return [];
    if (!retryIds) return quiz.questions;
    const wanted = new Set(retryIds);
    return quiz.questions.filter((q) => wanted.has(q.id));
  }, [quiz, retryIds]);
  const sections = useMemo(() => groupBySection(visibleQuestions), [visibleQuestions]);

  function exportAll() {
    const payload = {
      format: "handbook-ai-tutor/quiz-records@1",
      exported_at: new Date().toISOString(),
      source: { id: id, filename: source.data?.filename, title: source.data?.title },
      quiz: quiz ?? null,
      records: records.data?.records ?? [],
      current_submission: result ?? null,
    };
    downloadJson(`${slugify(source.data?.title || source.data?.filename, "quiz")}-records.json`, payload);
  }

  const importRecords = useMutation({
    mutationFn: async (mode: "merge" | "replace") => {
      const raw = (await pickJsonFile()) as Record<string, unknown>;
      // Accept either a full source bundle or the lighter quiz-records export.
      const bundle = ("format" in raw && raw.source && raw.records && !("notes" in raw)
        ? { ...raw, notes: [], knowledge: [], summary: null, quizzes: raw.quiz ? [raw.quiz] : [] }
        : raw) as Record<string, unknown>;
      return api<ImportResult>(`/api/v1/sources/${id}/import`, {
        method: "POST",
        body: JSON.stringify({ bundle, mode }),
      });
    },
    onSuccess: (res) => {
      setBanner({
        kind: "ok",
        text: `导入完成：新增题目组 ${res.quizzes_created}、提交记录 ${res.records_created}`,
      });
      void queryClient.invalidateQueries({ queryKey: ["quiz-records", id] });
      void queryClient.invalidateQueries({ queryKey: ["quiz", id] });
    },
    onError: (err) => setBanner({ kind: "err", text: err instanceof Error ? err.message : "导入失败" }),
  });

  // 当前正在展示的那一套（quizzes 为空时回退到显式选择项）。
  const activeQuizId = quiz?.id || selectedQuizId || "";
  const activeAttemptCount =
    records.data?.records.filter((r) => r.quiz_id === deleteTarget?.id).length ?? 0;
  // 提示文案。注意：作答记录来自另一个查询，loading / 失败时计数不可信，
  // 所以它只用来措辞，绝不用来决定控件是否可交互。
  const deleteAttemptHint = records.isLoading
    ? "正在读取该题组的提交记录…"
    : activeAttemptCount > 0
      ? `该题组有 ${activeAttemptCount} 条提交记录；勾选后一并清除，不勾选则仍保留在下方「提交记录存档」。`
      : "该题组暂无提交记录，勾选与否结果相同。";

  const removeQuiz = useMutation({
    mutationFn: (vars: { quizId: string; title: string; withAttempts: boolean }) =>
      api<QuizDeleteOut>(
        `/api/v1/quizzes/${vars.quizId}?with_attempts=${vars.withAttempts ? "true" : "false"}`,
        { method: "DELETE" }
      ),
    onSuccess: (report, vars) => {
      const remaining = (quizList.data?.quizzes || []).filter((q) => q.id !== report.quiz_id);
      const wasActive = activeQuizId === report.quiz_id;

      if (wasActive) {
        // 删掉的正是当前展示的这一套：先切到剩下的最新一套（没有则回到空态），
        // 再清空作答态，避免题目区 / 解析区指向已不存在的 quiz。
        setSelectedQuizId(remaining[0]?.id ?? null);
        setResult(null);
        setViewing(null);
        setRetryIds(null);
        setDrafts({});
        generate.reset();
      }

      setDeleteTarget(null);
      setDeleteWithAttempts(false);

      // 丢掉这套题已缓存的题目与作答，防止残留数据在新选择下闪现。
      queryClient.removeQueries({ queryKey: ["quiz", id, report.quiz_id] });
      void queryClient.invalidateQueries({ queryKey: ["quizzes", id] });
      void queryClient.invalidateQueries({ queryKey: ["quiz", id] });
      void queryClient.invalidateQueries({ queryKey: ["quiz-records", id] });

      setBanner({
        kind: "ok",
        text:
          `已删除题组「${vars.title}」：移除 ${report.deleted_questions} 道题目` +
          (vars.withAttempts
            ? `、${report.deleted_attempts} 条作答记录`
            : "，作答记录已保留在下方存档"),
      });
    },
    onError: (err) => {
      // 关掉弹层再报错，否则横幅会被遮罩挡住看不见。
      setDeleteTarget(null);
      setDeleteWithAttempts(false);
      setBanner({ kind: "err", text: err instanceof Error ? err.message : "删除失败" });
    },
  });

  function selectQuiz(quizId: string) {
    if (quizId === activeQuizId) return;
    setSelectedQuizId(quizId);
    setResult(null);
    setViewing(null);
    setRetryIds(null);
    setDrafts({});
  }

  function openDelete(item: QuizSummary) {
    setDeleteWithAttempts(false);
    setDeleteTarget(item);
  }

  return (
    <div className="min-h-screen">
      <AppHeader />
      <main className="mx-auto max-w-4xl space-y-6 px-4 pb-12 pt-6 sm:px-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-start gap-3">
            <span className="mt-0.5 flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-brand text-white shadow-soft">
              <ListChecks className="h-5 w-5" />
            </span>
            <div>
              <h1 className="text-2xl font-semibold tracking-tight">练习题</h1>
              <p className="text-sm text-muted-foreground">
                {source.data?.title || source.data?.filename || ""}
              </p>
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button asChild size="sm" variant="outline">
              <Link href={`/sources/${id}`}>
                <ArrowLeft />
                返回原文
              </Link>
            </Button>
            <Button
              size="sm"
              variant="gradient"
              onClick={() => generate.mutate()}
              pending={generate.isPending}
            >
              {generate.isPending ? "生成中…" : quiz ? "再生成一套" : "生成练习题"}
            </Button>
          </div>
        </div>
        {quizList.data && quizList.data.quizzes.length > 0 && (
          <Card>
            <CardHeader className="pb-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
                  <Layers className="h-4 w-4" />
                </span>
                <div>
                  <CardTitle className="text-base">历史题组</CardTitle>
                  <CardDescription>
                    共 {quizList.data.quizzes.length} 套。点标题切换查看，右侧可逐套删除；重新生成不会覆盖旧题组。
                  </CardDescription>
                </div>
              </div>
            </CardHeader>
            <CardContent className="space-y-2">
              {quizList.data.quizzes.map((item) => {
                const isActive = item.id === activeQuizId;
                return (
                  <div
                    key={item.id}
                    className={[
                      "flex flex-wrap items-center justify-between gap-2 rounded-lg border p-3 transition-colors",
                      isActive
                        ? "border-primary/40 bg-primary/[0.04]"
                        : "border-border/60 bg-card/50 hover:border-border hover:bg-card",
                    ].join(" ")}
                  >
                    <button
                      type="button"
                      className="min-w-0 flex-1 text-left"
                      onClick={() => selectQuiz(item.id)}
                      title={isActive ? "当前正在查看这套题" : "切换到这套题"}
                    >
                      <p
                        className={`truncate text-sm ${isActive ? "font-semibold" : "font-medium"}`}
                      >
                        {item.title}
                      </p>
                      <p className="text-xs text-muted-foreground">
                        {item.question_count} 题 · {item.sections.length} 个章节
                        {item.created_at ? ` · ${formatTime(item.created_at)}` : ""}
                      </p>
                    </button>
                    <div className="flex shrink-0 items-center gap-1">
                      {isActive && <Badge variant="soft">当前</Badge>}
                      <Button
                        size="sm"
                        variant="ghost"
                        className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                        onClick={() => openDelete(item)}
                        disabled={removeQuiz.isPending || generate.isPending}
                        title="删除这套练习题"
                      >
                        <Trash2 />
                        删除
                      </Button>
                    </div>
                  </div>
                );
              })}
            </CardContent>
          </Card>
        )}

        {banner && (
          <div
            className={`flex items-start justify-between gap-3 rounded-lg border px-3 py-2.5 text-sm animate-fade-down ${
              banner.kind === "ok"
                ? "border-success/30 bg-success/5"
                : "border-destructive/30 bg-destructive/5"
            }`}
          >
            <div className="flex items-start gap-2">
              {banner.kind === "ok" ? (
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success" />
              ) : (
                <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
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

        {quizQuery.isError && !quiz && (
          <Card>
            <CardContent className="flex items-center gap-3 py-8 text-sm text-muted-foreground">
              <Sparkles className="h-4 w-4 text-primary" />
              还没有练习题。点右上角「生成练习题」，系统会按章节生成混合题型（选择题 + 翻译 / 写作 / 口语）。
            </CardContent>
          </Card>
        )}

        {quiz && (
          <>
            <Card>
              <CardHeader className="pb-3">
                <div className="flex items-center gap-2">
                  <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
                    <ListChecks className="h-4 w-4" />
                  </span>
                  <div>
                    <CardTitle className="text-base">{quiz.title}</CardTitle>
                    <CardDescription>
                      {retryIds ? `错题重练 · ${visibleQuestions.length} 题` : `章节练习 · ${quiz.questions.length} 题`}
                      {" · "}
                      {quiz.sections.length} 个章节 · {quiz.prompt_version}。提交前不显示答案。
                    </CardDescription>
                  </div>
                </div>
              </CardHeader>
              <CardContent className="space-y-8">
                {sections.map(([section, items]) => (
                  <div key={section} className="space-y-4">
                    <div className="flex items-center gap-2 border-b pb-1.5">
                      <h3 className="text-sm font-semibold">{section}</h3>
                      <Badge variant="soft">{items.length} 题</Badge>
                    </div>
                    {items.map((q) => (
                      <QuestionInput
                        key={q.id}
                        question={q}
                        draft={drafts[q.id] || {}}
                        onChange={(d) => setDrafts((prev) => ({ ...prev, [q.id]: d }))}
                      />
                    ))}
                  </div>
                ))}
                <div className="flex items-center gap-3 border-t pt-2">
                  <Button variant="gradient" onClick={() => submit.mutate(quiz.id)} pending={submit.isPending}>
                    {submit.isPending ? "批改中…" : retryIds ? "提交错题重练" : "提交并生成解析"}
                    {!submit.isPending && <Send />}
                  </Button>
                  {retryIds && (
                    <Button
                      variant="outline"
                      onClick={() => {
                        setRetryIds(null);
                        setDrafts({});
                      }}
                    >
                      取消重练，回到全套
                    </Button>
                  )}
                  <span className="text-xs text-muted-foreground">
                    {retryIds
                      ? "只批改本次勾选的错题；原提交记录仍保留。"
                      : "提交后会记录时间，并为全部题目生成答案解析。重新生成不会删除旧题组。"}
                  </span>
                </div>
              </CardContent>
            </Card>

            {result && (
              <AnswerSheet
                title="本次提交"
                sourceId={id}
                structure={structure.data}
                onRetryFailed={() => {
                  const failed = result.results.filter((r) => !r.correct).map((r) => r.question_id);
                  if (!failed.length) return;
                  setRetryIds(failed);
                  setDrafts({});
                  setResult(null);
                  setBanner({ kind: "ok", text: `已载入 ${failed.length} 道错题，答完后再次提交。` });
                }}
                attempt={result}
                onExport={() => {
                  downloadJson(
                    `${slugify(quiz.title)}-attempt-${result.id.slice(0, 8)}.json`,
                    result
                  );
                }}
                onExportSection={(sectionTitle) => {
                  const payload = {
                    format: "handbook-ai-tutor/quiz-records@1",
                    exported_at: new Date().toISOString(),
                    section: sectionTitle,
                    source: { id, filename: source.data?.filename, title: source.data?.title },
                    quiz: {
                      ...quiz,
                      questions: quiz.questions.filter(
                        (q) => (q.section_title || "General") === sectionTitle
                      ),
                    },
                    records: [
                      {
                        quiz_title: quiz.title,
                        score: result.score,
                        passed: result.passed,
                        submitted_at: result.submitted_at,
                        graded_count: result.graded_count,
                        details: result.results
                          .filter((r) => (r.section_title || "General") === sectionTitle)
                          .map((r) => ({
                            question_id: r.question_id,
                            ordinal: r.ordinal,
                            section_title: r.section_title,
                            question_type: r.question_type,
                            question: r.question,
                            selected_index: r.selected_index,
                            text_answer: r.text_answer,
                            correct_index: r.correct_index,
                            correct: r.correct,
                            verdict: r.verdict,
                            score: r.score,
                            reference_answer: r.reference_answer,
                            ai_explanation: r.ai_explanation,
                          })),
                      },
                    ],
                  };
                  downloadJson(`${slugify(quiz.title)}-${slugify(sectionTitle)}.json`, payload);
                }}
              />
            )}
          </>
        )}

        <Card>
          <CardHeader className="pb-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
                  <History className="h-4 w-4" />
                </span>
                <div>
                  <CardTitle className="text-base">提交记录存档</CardTitle>
                  <CardDescription>每条记录含提交时间、总分、分章节得分与逐题解析。</CardDescription>
                </div>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button size="sm" variant="outline" onClick={exportAll} disabled={!records.data?.records.length}>
                  <Download />
                  导出全部
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => importRecords.mutate("merge")}
                  pending={importRecords.isPending}
                >
                  <Upload />
                  导入记录
                </Button>
              </div>
            </div>
          </CardHeader>
          <CardContent className="space-y-2">
            {records.isLoading && (
              <div className="space-y-2">
                {Array.from({ length: 3 }).map((_, i) => (
                  <div key={i} className="skeleton h-16 w-full" />
                ))}
              </div>
            )}
            {!records.isLoading && !records.data?.records.length && (
              <p className="text-sm text-muted-foreground">还没有提交记录。</p>
            )}
            {(records.data?.records || []).map((record) => (
              <RecordRow
                key={record.id}
                record={record}
                onOpen={async () => {
                  try {
                    const attempt = await api<Attempt>(`/api/v1/quiz-attempts/${record.id}`);
                    setViewing(attempt);
                    setResult(null);
                  } catch (err) {
                    setBanner({
                      kind: "err",
                      text: err instanceof Error ? err.message : "读取记录失败",
                    });
                  }
                }}
                onExport={() =>
                  downloadJson(
                    `${slugify(record.quiz_title)}-${formatTime(record.submitted_at).replace(/[: ]/g, "-")}.json`,
                    record
                  )
                }
              />
            ))}
          </CardContent>
        </Card>

        {viewing && (
          <AnswerSheet
            title="历史记录"
            sourceId={id}
            structure={structure.data}
            attempt={viewing}
            onRetryFailed={() => {
              const failed = viewing.results.filter((r) => !r.correct).map((r) => r.question_id);
              if (!failed.length) return;
              setRetryIds(failed);
              setDrafts({});
              setViewing(null);
              setBanner({ kind: "ok", text: `已载入 ${failed.length} 道错题，答完后再次提交。` });
            }}
            onClose={() => setViewing(null)}
            onExport={() => downloadJson(`attempt-${viewing.id.slice(0, 8)}.json`, viewing)}
          />
        )}
      </main>

      {deleteTarget && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-foreground/40 p-4 backdrop-blur-sm animate-fade-in"
          onClick={() => {
            if (!removeQuiz.isPending) {
              setDeleteTarget(null);
              setDeleteWithAttempts(false);
            }
          }}
        >
          <Card
            className="w-full max-w-md shadow-lift animate-scale-in"
            onClick={(e) => e.stopPropagation()}
          >
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                <Trash2 className="h-4 w-4 text-destructive" />
                删除这套练习题？
              </CardTitle>
              <CardDescription className="break-all">
                「{deleteTarget.title}」· {deleteTarget.question_count} 题 ·{" "}
                {deleteTarget.sections.length} 个章节
                {deleteTarget.created_at ? ` · ${formatTime(deleteTarget.created_at)}` : ""}
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <label className="flex cursor-pointer items-start gap-2.5 rounded-lg border border-border/60 bg-muted/30 p-3 text-sm transition-colors hover:border-border hover:bg-muted/50">
                <input
                  type="checkbox"
                  className="mt-0.5 h-4 w-4 shrink-0 cursor-pointer accent-destructive"
                  checked={deleteWithAttempts}
                  onChange={(e) => setDeleteWithAttempts(e.target.checked)}
                  disabled={removeQuiz.isPending}
                />
                <span>
                  <span className="font-medium">同时删除该题组的作答记录</span>
                  <span className="mt-0.5 block text-xs text-muted-foreground">
                    {deleteAttemptHint}
                  </span>
                </span>
              </label>
              <p className="text-xs text-muted-foreground">
                题目与解析会随题组一并删除，且无法恢复；其他题组与原文不受影响。
              </p>
              <div className="flex justify-end gap-2">
                <Button
                  variant="outline"
                  disabled={removeQuiz.isPending}
                  onClick={() => {
                    setDeleteTarget(null);
                    setDeleteWithAttempts(false);
                  }}
                >
                  取消
                </Button>
                <Button
                  variant="destructive"
                  pending={removeQuiz.isPending}
                  onClick={() =>
                    removeQuiz.mutate({
                      quizId: deleteTarget.id,
                      title: deleteTarget.title,
                      withAttempts: deleteWithAttempts,
                    })
                  }
                >
                  {removeQuiz.isPending ? "删除中…" : "确认删除"}
                </Button>
              </div>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}

function groupBySection(questions: QuizQuestion[]): [string, QuizQuestion[]][] {
  const buckets = new Map<string, QuizQuestion[]>();
  for (const q of [...questions].sort((a, b) => a.ordinal - b.ordinal)) {
    const key = q.section_title || "General";
    const list = buckets.get(key);
    if (list) list.push(q);
    else buckets.set(key, [q]);
  }
  return Array.from(buckets.entries());
}

function QuestionInput({
  question,
  draft,
  onChange,
}: {
  question: QuizQuestion;
  draft: Draft;
  onChange: (d: Draft) => void;
}) {
  const label = QUESTION_TYPE_LABEL[question.question_type] || question.question_type;
  const isChoice = question.question_type === "choice" && question.options.length > 0;

  return (
    <div className="space-y-3 rounded-xl border border-border/60 bg-card/50 p-4 transition-all hover:border-border">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="soft">{label}</Badge>
        <p className="font-medium leading-relaxed">
          {question.ordinal + 1}. {question.question}
        </p>
      </div>
      {question.instructions && (
        <p className="text-xs text-muted-foreground">{question.instructions}</p>
      )}
      {question.scoring_note && (
        <p className="rounded-md border border-warning/30 bg-warning/10 px-2 py-1 text-xs">
          {question.scoring_note}
        </p>
      )}
      {isChoice ? (
        <div className="space-y-1.5">
          {question.options.map((option, index) => {
            const selected = draft.selected_index === index;
            return (
              <label
                key={index}
                className={[
                  "flex cursor-pointer items-start gap-3 rounded-lg border px-3 py-2 text-sm",
                  "transition-all duration-150",
                  selected
                    ? "border-primary bg-primary/5 ring-1 ring-primary/30"
                    : "border-border/60 bg-background hover:border-primary/40 hover:bg-primary/[0.03]",
                ].join(" ")}
              >
                <span
                  className={[
                    "mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border-2 transition-colors",
                    selected ? "border-primary bg-primary" : "border-muted-foreground/40",
                  ].join(" ")}
                >
                  {selected && <span className="h-1.5 w-1.5 rounded-full bg-white" />}
                </span>
                <input
                  type="radio"
                  name={question.id}
                  className="sr-only"
                  checked={selected}
                  onChange={() => onChange({ selected_index: index })}
                />
                <span className="leading-relaxed">
                  <span className="font-mono text-xs text-muted-foreground">
                    {String.fromCharCode(65 + index)}.
                  </span>{" "}
                  {option}
                </span>
              </label>
            );
          })}
        </div>
      ) : (
        <Textarea
          placeholder="在这里输入你的作答（翻译 / 写作 / 口语转写）…"
          value={draft.text_answer || ""}
          onChange={(e) => onChange({ text_answer: e.target.value })}
        />
      )}
    </div>
  );
}

function AnswerSheet({
  title,
  attempt,
  sourceId,
  structure,
  onClose,
  onExport,
  onExportSection,
  onRetryFailed,
}: {
  title: string;
  attempt: Attempt;
  sourceId?: string;
  structure?: SourceStructure;
  onClose?: () => void;
  onExport: () => void;
  onExportSection?: (sectionTitle: string) => void;
  onRetryFailed?: () => void;
}) {
  const sections = useMemo(() => {
    const buckets = new Map<string, QuestionResult[]>();
    for (const r of [...attempt.results].sort((a, b) => a.ordinal - b.ordinal)) {
      const key = r.section_title || "General";
      const list = buckets.get(key);
      if (list) list.push(r);
      else buckets.set(key, [r]);
    }
    return Array.from(buckets.entries());
  }, [attempt]);

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-success/15 text-success">
              <CheckCircle2 className="h-4 w-4" />
            </span>
            <div>
              <CardTitle className="text-base">{title} · 答案解析</CardTitle>
              <CardDescription>
                提交时间 {formatTime(attempt.submitted_at)} · 得分 {Math.round(attempt.score * 100)}%
                {attempt.passed ? " · 通过" : " · 继续加油"} · AI 解析覆盖 {attempt.graded_count} 题
              </CardDescription>
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            {onRetryFailed && attempt.results.some((r) => !r.correct) && (
              <Button size="sm" variant="gradient" onClick={onRetryFailed}>
                重做错题
              </Button>
            )}
            <Button size="sm" variant="outline" onClick={onExport}>
              <Download />
              导出本次记录
            </Button>
            {onClose && (
              <Button size="sm" variant="ghost" onClick={onClose}>
                收起
              </Button>
            )}
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="flex flex-wrap gap-2">
          {attempt.sections.map((s) => (
            <Badge
              key={s.section_title}
              variant={s.score >= 0.6 ? "success" : "warning"}
            >
              {s.section_title}：{s.correct}/{s.total}（{Math.round(s.score * 100)}%）
            </Badge>
          ))}
        </div>

        {sections.map(([sectionTitle, rows]) => (
          <div key={sectionTitle} className="space-y-3">
            <div className="flex items-center justify-between gap-2 border-b pb-1.5">
              <h3 className="text-sm font-semibold">{sectionTitle}</h3>
              {onExportSection && (
                <Button size="sm" variant="ghost" onClick={() => onExportSection(sectionTitle)}>
                  <FileJson />
                  导出本章节
                </Button>
              )}
            </div>
            {rows.map((row) => (
              <ResultRow key={row.question_id} row={row} sourceId={sourceId} structure={structure} />
            ))}
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

function citationHref(
  sourceId: string | undefined,
  chunkId: string,
  structure?: SourceStructure
): string | null {
  if (!sourceId) return null;
  const chunk = structure?.chunks.find((c) => c.id === chunkId);
  if (!chunk) return `/sources/${sourceId}`;
  if (chunk.start_time != null) return `/sources/${sourceId}?t=${chunk.start_time}`;
  const page = chunk.page_number ?? chunk.printed_page;
  if (page != null) return `/sources/${sourceId}?page=${page}`;
  return `/sources/${sourceId}`;
}

function ResultRow({
  row,
  sourceId,
  structure,
}: {
  row: QuestionResult;
  sourceId?: string;
  structure?: SourceStructure;
}) {
  const label = QUESTION_TYPE_LABEL[row.question_type] || row.question_type;
  const isChoice = row.question_type === "choice";
  const userAnswer = isChoice
    ? row.selected_index === null || row.selected_index === undefined
      ? "未作答"
      : String.fromCharCode(65 + row.selected_index)
    : row.text_answer || "未作答";
  const rightAnswer = isChoice
    ? row.correct_index === null || row.correct_index === undefined
      ? "—"
      : String.fromCharCode(65 + row.correct_index)
    : row.reference_answer || "—";

  return (
    <div
      className={[
        "space-y-2 rounded-xl border p-4 transition-colors",
        row.correct
          ? "border-success/30 bg-success/5"
          : "border-destructive/30 bg-destructive/5",
      ].join(" ")}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="soft">{label}</Badge>
        {row.correct ? (
          <Badge variant="success">
            <CheckCircle2 className="h-3 w-3" />
            正确
          </Badge>
        ) : (
          <Badge variant="destructive">
            <XCircle className="h-3 w-3" />
            {row.score !== null ? `${Math.round((row.score || 0) * 100)}%` : "待改进"}
          </Badge>
        )}
        <p className="font-medium text-sm leading-relaxed">
          {row.ordinal + 1}. {row.question}
        </p>
      </div>
      {row.scoring_note && (
        <p className="rounded-md border border-warning/30 bg-warning/10 px-2 py-1 text-xs">
          {row.scoring_note}
        </p>
      )}
      {row.chunk_ids.length > 0 && (
        <div className="flex flex-wrap gap-1.5 text-xs">
          {row.chunk_ids.map((chunkId) => {
            const href = citationHref(sourceId, chunkId, structure);
            return href ? (
              <Link
                key={chunkId}
                href={href}
                className="rounded-full bg-primary/10 px-2 py-0.5 text-primary underline-offset-4 hover:underline"
              >
                跳到原文
              </Link>
            ) : null;
          })}
        </div>
      )}
      <p className="text-sm">
        <span className="text-muted-foreground">你的作答：</span>
        <span className="whitespace-pre-wrap">{userAnswer}</span>
      </p>
      {!row.correct && (
        <p className="text-sm">
          <span className="text-muted-foreground">参考答案：</span>
          <span className="whitespace-pre-wrap">{rightAnswer}</span>
        </p>
      )}
      {row.verdict && (
        <p className="text-sm">
          <span className="text-muted-foreground">评语：</span>
          <span className="whitespace-pre-wrap">{row.verdict}</span>
        </p>
      )}
      <div className="rounded-md bg-muted/50 p-3 text-sm">
        <span className="text-xs text-muted-foreground">解析：</span>
        <p className="mt-1 whitespace-pre-wrap">{row.ai_explanation || row.explanation || "（无）"}</p>
      </div>
    </div>
  );
}

function RecordRow({
  record,
  onOpen,
  onExport,
}: {
  record: QuizRecord;
  onOpen: () => void;
  onExport: () => void;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border/60 bg-card/50 p-3 transition-colors hover:border-border hover:bg-card">
      <div className="min-w-0">
        <p className="truncate text-sm font-medium">{record.quiz_title}</p>
        <p className="text-xs text-muted-foreground">
          {formatTime(record.submitted_at)} · 得分 {Math.round(record.score * 100)}%
          {record.passed ? " · 通过" : ""} · 解析 {record.graded_count} 题
        </p>
        {record.sections.length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1">
            {record.sections.map((s) => (
              <Badge
                key={s.section_title}
                variant={s.score >= 0.6 ? "success" : "warning"}
              >
                {s.section_title} {s.correct}/{s.total}
              </Badge>
            ))}
          </div>
        )}
      </div>
      <div className="flex shrink-0 gap-1">
        <Button size="sm" variant="ghost" onClick={onOpen}>
          <Eye />
          查看解析
        </Button>
        <Button size="sm" variant="ghost" onClick={onExport}>
          <Download />
          导出
        </Button>
      </div>
    </div>
  );
}
