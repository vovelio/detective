// Only client-visible views. The server's persistence envelope is never a client type.
export type CaseFormatVersion = "1.0.0";
export type Phase =
  | "LOBBY"
  | "INTRO"
  | "PLAYING"
  | "DISCUSSION"
  | "ANSWERING"
  | "REVIEW"
  | "RESULTS"
  | "FINISHED";
export interface Player {
  id: string;
  nickname: string;
  seat?: number;
  hand_count?: number;
}
export interface RoomInfo {
  code: string;
  member_id: string;
  host_id: string;
  selected_case_id: string | null;
  phase: Phase;
  revision: number;
  players: Player[];
}
export interface CaseMeta {
  title: string;
  author: string;
  summary: string;
  difficulty: "easy" | "medium" | "hard";
  estimated_minutes: number;
  min_players: number;
  max_players: number;
}
export interface CaseInfo {
  id: string;
  meta: CaseMeta;
}
export interface ImportIssue {
  path: string;
  code: string;
}
export interface ImportReceipt {
  case: CaseInfo;
  already_imported: boolean;
}
export interface ImportState {
  status: "idle" | "uploading" | "success" | "error" | "uncertain";
  message: string;
  issues: ImportIssue[];
}
export interface Asset {
  id: string;
  alt: string;
  mime: string;
  path: string;
}
export type Block =
  | {
      type: "text";
      segments: { text: string; shareable?: "highlight" | "clip" }[];
    }
  | { type: "image"; asset_id: string; caption: string };
export interface CardData {
  id: string;
  title: string;
  blocks: Block[];
  kind?: string;
  role?: "essential" | "supporting" | "red_herring";
  logic_notes?: string;
}
export interface Question {
  id: string;
  text: string;
  options: { id: string; text: string }[];
}
export interface Answer {
  question_id: string;
  option_id: string;
  reviewed_correct?: boolean | null;
}
export interface AnswerKey {
  id: string;
  correct_option_id: string;
  explanation: string;
  evidence_card_ids: string[];
}
export interface Point {
  x: number;
  y: number;
}
export type BoardObject = { id: string; version: number } & (
  | {
      kind: "card_position";
      payload: { card_id: string; x: number; y: number; rotation: number };
    }
  | { kind: "stroke"; payload: { points: Point[] } }
  | { kind: "arrow"; payload: { from: Point; to: Point } }
  | { kind: "note"; payload: { x: number; y: number; text: string } }
);
export type BoardMutation =
  | {
      op: "card_transform";
      card_id: string;
      x: number;
      y: number;
      rotation: number;
      expected_version: number;
    }
  | { op: "stroke_create"; object_id: string; points: Point[] }
  | {
      op: "arrow_update";
      object_id: string;
      expected_version: number;
      from: Point;
      to: Point;
    }
  | { op: "arrow_create"; object_id: string; from: Point; to: Point }
  | { op: "note_create"; object_id: string; x: number; y: number; text: string }
  | {
      op: "note_update";
      object_id: string;
      expected_version: number;
      x: number;
      y: number;
      text: string;
    }
  | { op: "object_delete"; object_id: string; expected_version: number };
export interface CardPreview {
  card_id: string;
  x: number;
  y: number;
  rotation: number;
  version: number;
}
export interface GameSnapshot {
  board?: { objects: BoardObject[] };
  game_id: string;
  phase: Exclude<Phase, "LOBBY">;
  revision: number;
  host_id: string;
  players: Player[];
  active_player_id: string | null;
  turn_order: string[];
  deck_count: number;
  discarded_count: number;
  case: CaseInfo;
  intro: { text: string; public_goal: string };
  published: { card: CardData; position: { x: number; y: number } }[];
  finish_proposed_by: string | null;
  answers: Answer[];
  questions?: Question[];
  assets: Asset[];
  private: {
    hand: CardData[];
    assets: Asset[];
    answer_key?: AnswerKey[];
    reviewed_answers?: Answer[];
  };
  answer_key?: AnswerKey[];
  cards?: CardData[];
  discarded?: string[];
  solution?: {
    full_story: string;
    timeline: { when: string; event: string }[];
    reveal_notes: string;
  };
  result?: {
    score: number;
    correct_answers: number;
    irrelevant_revealed: number;
    discarded: number;
    missing_discards: number;
    answer_points: number;
    irrelevant_penalty: number;
    missing_discard_penalty: number;
  };
}
export type Snapshot = RoomInfo | GameSnapshot;
export function isGame(value: Snapshot): value is GameSnapshot {
  return value.phase !== "LOBBY";
}
export interface SessionInfo {
  csrf_token: string;
  rooms: RoomInfo[];
}
export interface Payloads {
  board_mutation: BoardMutation;
  begin_play: Record<string, never>;
  play_card: {
    card_id: string;
    mode: "reveal" | "discard";
    confirmed: boolean;
    x?: number;
    y?: number;
  };
  propose_finish: Record<string, never>;
  confirm_finish: Record<string, never>;
  answer: { question_id: string; option_id: string };
  submit_answers: Record<string, never>;
  review_answer: { question_id: string; correct: boolean };
  finish_game: Record<string, never>;
}
export type ActionType = keyof Payloads;
export type ActionBody = {
  [K in ActionType]: {
    type: K;
    payload: Payloads[K];
    client_action_id: string;
    expected_revision: number;
  };
}[ActionType];
export type StartBody = { client_action_id: string; expected_revision: number };
export interface Receipt {
  client_action_id: string;
  revision: number;
  phase: Phase;
}
export type Pending = { code: string; memberId: string } & (
  | { kind: "start"; body: StartBody }
  | { kind: "action"; body: ActionBody }
);
