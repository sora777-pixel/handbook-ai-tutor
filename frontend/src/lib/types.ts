// Shared API contract types. Mirrors backend/app/domain/schemas.py.

/** What the layout analyser decided a piece of the document is. */
export type ContentType =
  | "body"
  | "heading"
  | "caption"
  | "formula"
  | "table"
  | "figure"
  | "reference"
  | "outline";

export const CONTENT_TYPE_LABELS: Record<string, string> = {
  body: "正文",
  heading: "标题",
  caption: "图注/表注",
  formula: "公式",
  table: "表格",
  figure: "图内文字",
  reference: "参考文献",
  outline: "目录大纲",
};

export type Source = {
  id: string;
  filename: string;
  content_type: string;
  kind: string;
  status: string;
  title: string | null;
  byte_size: number;
  created_at: string;
  task_id?: string | null;
  error_message?: string | null;
  /** printed_page - physical page_number, recovered from the running heads. */
  page_offset?: number | null;
  page_count?: number | null;
  /** Latest ingest task message: OCR / STT / mock provenance. */
  extraction_note?: string | null;
  task_status?: string | null;
  task_progress?: number | null;
  task_step?: string | null;
  task_message?: string | null;
};

export type OutlineEntry = {
  level: number;
  number: string;
  title: string;
  /** Page-based sources (PDF). Recordings leave this at 0. */
  page_number?: number | null;
  printed_page?: number | null;
  page_end?: number | null;
  /** Recording only: the position the player should seek to, in seconds. */
  start_time?: number | null;
  end_time?: number | null;
};

export type StructureChunk = {
  id: string;
  ordinal: number;
  content_type: string;
  section_title: string | null;
  page_number: number | null;
  printed_page: number | null;
  locator: string | null;
  heading_level: number | null;
  start_time?: number | null;
  end_time?: number | null;
  preview: string;
};

export type SourceStructure = {
  source_id: string;
  /** pdf | video | image — decides which half of the panel is shown. */
  kind?: string;
  /** Recording only: total length in seconds. */
  duration?: number | null;
  page_offset: number | null;
  page_count: number | null;
  outline: OutlineEntry[];
  content_types: Record<string, number>;
  chunks: StructureChunk[];
  chunk_total: number;
  section_index: Record<string, number[]>;
};

export type Citation = {
  chunk_id: string;
  source_id: string;
  locator: string | null;
  page_number: number | null;
  printed_page: number | null;
  section_title: string | null;
  content_type: string;
  start_time: number | null;
  end_time: number | null;
  quote: string;
  score: number | null;
};

export type Task = {
  id: string;
  source_id: string;
  kind: string;
  status: string;
  progress: number;
  step: string;
  message: string | null;
};

export type Summary = {
  source_id: string;
  title: string;
  overview: string;
  outline: string[];
  prompt_version: string;
};

export type Knowledge = {
  id: string;
  title: string;
  summary: string;
  key_terms: string[];
  chunk_ids: string[];
};

export type Note = {
  id: string;
  source_id: string;
  title: string;
  content: string;
  origin: string;
  anchor: string | null;
  ordinal: number;
  created_at: string;
  updated_at: string | null;
};

export type QuestionType = "choice" | "translation" | "writing" | "speaking";

export type QuizQuestion = {
  id: string;
  ordinal: number;
  question: string;
  options: string[];
  question_type: QuestionType;
  section_title: string;
  instructions: string;
  scoring_note?: string;
};

export type Quiz = {
  id: string;
  source_id: string;
  title: string;
  prompt_version: string;
  created_at: string | null;
  sections: string[];
  questions: QuizQuestion[];
};

export type QuizSummary = {
  id: string;
  source_id: string;
  title: string;
  prompt_version: string;
  created_at: string | null;
  question_count: number;
  sections: string[];
};

export type QuizList = {
  source_id: string;
  quizzes: QuizSummary[];
};

/** Report for deleting one generated quiz version. */
export type QuizDeleteOut = {
  quiz_id: string;
  source_id: string;
  deleted_questions: number;
  deleted_attempts: number;
  with_attempts: boolean;
};

export type QuestionResult = {
  question_id: string;
  ordinal: number;
  section_title: string;
  question_type: QuestionType;
  question: string;
  selected_index: number | null;
  text_answer: string;
  correct_index: number | null;
  correct: boolean;
  verdict: string;
  score: number | null;
  reference_answer: string;
  ai_explanation: string;
  explanation: string;
  chunk_ids: string[];
  scoring_note?: string;
};

export type SectionResult = {
  section_title: string;
  total: number;
  correct: number;
  score: number;
};

export type Attempt = {
  id: string;
  quiz_id: string;
  score: number;
  passed: boolean;
  submitted_at: string;
  graded_count: number;
  results: QuestionResult[];
  sections: SectionResult[];
};

export type QuizRecord = {
  id: string;
  quiz_id: string;
  quiz_title: string;
  source_id: string;
  score: number;
  passed: boolean;
  submitted_at: string;
  graded_count: number;
  sections: SectionResult[];
};

export type SourceDeleteReport = {
  source_id: string;
  filename: string;
  storage_key: string;
  storage_deleted: boolean;
  storage_error: string | null;
  rows_deleted: Record<string, number>;
  total_rows_deleted: number;
};

export type RegenerateOut = {
  source_id: string;
  provider: string;
  model: string;
  title: string;
  knowledge_points: number;
  task: string;
};

export type ImportResult = {
  notes_created: number;
  notes_skipped: number;
  quizzes_created: number;
  records_created: number;
  summary_restored: boolean;
  knowledge_restored: number;
  tutor_created?: number;
  tutor_skipped?: number;
  mode: string;
};

export type SourceBundle = {
  format: string;
  exported_at: string;
  source: Record<string, unknown>;
  summary: Record<string, unknown> | null;
  knowledge: Record<string, unknown>[];
  notes: Record<string, unknown>[];
  quizzes: Record<string, unknown>[];
  records: Record<string, unknown>[];
  tutor?: Record<string, unknown>[];
};

export type RouteRow = {
  task: string;
  modality: string;
  provider: string;
  model: string | null;
  base_url: string | null;
  api_key_env: string | null;
  api_key_present: boolean | null;
  ready: boolean;
  error: string | null;
  note: string | null;
};

export type OcrSummary = {
  mode: string;
  requested_language: string;
  engine: string;
  ready: boolean;
  reason: string | null;
  installed_languages?: string[];
  vision_fallback_ready: boolean;
  vision_fallback_reason: string | null;
};

/** One configurable runtime location (storage root, providers.yaml). */
export type PathEntry = {
  key: string;
  env_var: string;
  label: string;
  kind: "directory" | "file";
  hint: string;
  /** Value the backend is actually using right now. */
  current: string;
  /** Pre-generated location inside the project folder. */
  default: string;
  /** What runtime.json holds, or null when the default is in force. */
  custom_value: string | null;
  source: "default" | "custom" | "env";
  /** True when a process environment variable pins it — the UI cannot change it. */
  locked_by_env: boolean;
  exists: boolean;
  /** Set when the entry is currently irrelevant (e.g. object-storage backend). */
  note: string | null;
  detail: Record<string, unknown>;
};

export type PathsOut = {
  paths: PathEntry[];
  overrides_file: string;
  overrides: Record<string, string>;
  overrides_applied: Record<string, string>;
  changed: string[];
  warnings: string[];
  restart_required: boolean;
};

export type RoutesOut = {
  default_provider: string;
  providers_config_path: string;
  routes: RouteRow[];
  ocr?: OcrSummary | null;
};

export type LlmTestOut = {
  ok: boolean;
  task: string;
  modality: string;
  provider: string | null;
  model: string | null;
  reply: string | null;
  error: string | null;
};

/** One editable field of the hand-entered Embedding API. `value` is never the key. */
export type EmbedField = {
  key: "embed_api_base" | "embed_api_key" | "embed_model";
  env_var: string;
  label: string;
  hint: string;
  placeholder: string;
  value: string;
  /** Masked preview of an already-saved secret, e.g. "sk-…4f2a". */
  hint_value?: string | null;
  locked_by_env: boolean;
};

export type EmbedOut = {
  fields: EmbedField[];
  /** Address + model were both filled in. */
  configured: boolean;
  active: boolean;
  /** Provider that will actually serve vectors right now. */
  effective_provider: string;
  effective_model: string;
  note: string | null;
  overrides_file: string;
  changed: string[];
  warnings: string[];
};

export type UsageTaskRow = {
  task: string;
  provider: string;
  model: string;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
};

export type UsageSourceRow = {
  source_id: string | null;
  source_title: string | null;
  source_filename: string | null;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  calls: number;
  by_task: UsageTaskRow[];
};

export type UsageOut = {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  calls: number;
  by_source: UsageSourceRow[];
  by_task: UsageTaskRow[];
};

export type EmbedTestOut = {
  ok: boolean;
  provider: string | null;
  model: string | null;
  dim: number;
  note: string | null;
  error: string | null;
};
