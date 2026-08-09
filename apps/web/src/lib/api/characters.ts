/**
 * Character endpoints — proseforge/api/routes/characters.py (work-mode
 * projects only; chat projects 404).
 *
 * Confirmed shapes:
 *   GET    /api/v1/projects/{project_id}/characters -> Character[]
 *   POST   same  {name, aliases?, summary?, role?} -> 201 Character, 409 on duplicate name
 *   PATCH  /api/v1/projects/{project_id}/characters/{character_id}
 *          {name?, aliases?, summary?, role?, status?} -> Character
 *          (a manual edit promotes source to "user" server-side)
 *   DELETE same -> 204
 */
import { request } from "./client";

export interface Character {
  id: string;
  name: string;
  aliases: string[];
  summary: string;
  /** Free-form role tag, e.g. 主角 / 反派. */
  role: string;
  first_seen_chapter: number | null;
  last_seen_chapter: number | null;
  status: string;
  /** "auto" = extracted by the AI, "user" = confirmed/edited by hand. */
  source: "user" | "auto";
  confidence: number | null;
}

export interface CharacterInput {
  name: string;
  aliases?: string[];
  summary?: string;
  role?: string;
}

export interface CharacterUpdate {
  name?: string;
  aliases?: string[];
  summary?: string;
  role?: string;
  status?: string;
}

export function listCharacters(projectId: string): Promise<Character[]> {
  return request<Character[]>(`/api/v1/projects/${projectId}/characters`);
}

export function createCharacter(projectId: string, input: CharacterInput): Promise<Character> {
  return request<Character>(`/api/v1/projects/${projectId}/characters`, { method: "POST", body: input });
}

export function updateCharacter(projectId: string, characterId: string, input: CharacterUpdate): Promise<Character> {
  return request<Character>(`/api/v1/projects/${projectId}/characters/${characterId}`, {
    method: "PATCH",
    body: input,
  });
}

export function deleteCharacter(projectId: string, characterId: string): Promise<void> {
  return request<void>(`/api/v1/projects/${projectId}/characters/${characterId}`, { method: "DELETE" });
}
