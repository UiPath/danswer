import { ToolSnapshot } from "@/lib/tools/interfaces";
import { DocumentSet, MinimalUserSnapshot } from "@/lib/types";

export interface StarterMessage {
  name: string;
  description: string | null;
  message: string;
}

export interface Prompt {
  id: number;
  name: string;
  description: string;
  system_prompt: string;
  task_prompt: string;
  include_citations: boolean;
  datetime_aware: boolean;
  default_prompt: boolean;
}

export interface Persona {
  id: number;
  name: string;
  // Optional user-friendly label shown in the chat UI; falls back to `name`.
  display_name?: string | null;
  owner: MinimalUserSnapshot | null;
  is_visible: boolean;
  is_public: boolean;
  display_priority: number | null;
  description: string;
  // Router-only guidance for the auto-routed Search tab; never shown to users.
  routing_instructions?: string | null;
  // Comma-separated keywords that deterministically route to this assistant.
  routing_keywords?: string | null;
  // Newline-separated intent phrases for the semantic pre-route (one per line).
  routing_intents?: string | null;
  document_sets: DocumentSet[];
  prompts: Prompt[];
  tools: ToolSnapshot[];
  num_chunks?: number;
  llm_relevance_filter?: boolean;
  llm_filter_extraction?: boolean;
  rerank_enabled?: boolean;
  llm_model_provider_override?: string;
  llm_model_version_override?: string;
  starter_messages: StarterMessage[] | null;
  default_persona: boolean;
  users: MinimalUserSnapshot[];
  groups: number[];
}
