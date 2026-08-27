/** The message composer.
 *
 *  The skill selector is the product's answer to unreliable routing: the router
 *  infers intent from phrasing, but a user who *knows* what they want should
 *  never have to phrase their way into it. "Auto" is the default and covers the
 *  conversational case; the explicit modes are a guarantee.
 */

import { useEffect, useRef, useState } from "react";
import type { SkillName } from "../types";

interface Props {
  disabled: boolean;
  busy: boolean;
  onSend: (message: string, skill: SkillName | null) => void;
}

const SKILLS: Array<{ value: SkillName | null; label: string; hint: string }> = [
  { value: null, label: "Auto", hint: "Infer what you want from the message" },
  { value: "qa", label: "Answer", hint: "A grounded answer with citations" },
  {
    value: "ship30_essay",
    label: "Essay",
    hint: "A ~1,250 word Ship 30 for 30 essay",
  },
  {
    value: "artifact",
    label: "Artifact",
    hint: "A rendered Markdown or HTML document",
  },
];

export function Composer({ disabled, busy, onSend }: Props) {
  const [value, setValue] = useState("");
  const [skill, setSkill] = useState<SkillName | null>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);

  // Grow with content up to a ceiling, so a long brief is visible while typing
  // without the composer eating the conversation.
  useEffect(() => {
    const el = textarea.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
  }, [value]);

  function submit() {
    const trimmed = value.trim();
    if (!trimmed || disabled || busy) return;
    onSend(trimmed, skill);
    setValue("");
  }

  return (
    <form
      className="composer"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <div className="composer__skills" role="radiogroup" aria-label="Skill">
        {SKILLS.map((option) => (
          <button
            key={option.label}
            type="button"
            role="radio"
            aria-checked={skill === option.value}
            title={option.hint}
            className={`skill-pill ${skill === option.value ? "is-active" : ""}`}
            onClick={() => setSkill(option.value)}
          >
            {option.label}
          </button>
        ))}
      </div>

      <div className="composer__row">
        <label className="sr-only" htmlFor="composer-input">
          Ask about product and growth
        </label>
        <textarea
          id="composer-input"
          ref={textarea}
          className="composer__input"
          rows={1}
          value={value}
          disabled={disabled}
          placeholder={
            disabled
              ? "Start a new chat to begin"
              : "Ask about product, growth, hiring, AI…  (Enter to send, Shift+Enter for a new line)"
          }
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
        />
        <button
          type="submit"
          className="composer__send"
          disabled={disabled || busy || !value.trim()}
        >
          {busy ? "Thinking…" : "Send"}
        </button>
      </div>
    </form>
  );
}
