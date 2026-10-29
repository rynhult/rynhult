import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { post } from "../lib/api";
import { keepBriefDraft, readBriefDraft, type BriefDraft, type SavedBriefEvent, type Version } from "../lib/projectBrief";
import { useSingleMutation as useMutation } from "../lib/mutations";
import type { TeamGrant } from "./OrchestratorFields";
import { Button, ErrorNote } from "./ui";
import { useUnsavedBrief } from "./UnsavedBriefGuard";

export interface BriefLaunchChoice { projectId: number; draft: BriefDraft; nextVersion: number; exportPath: string }

export function BriefLaunchReview({ choice, grant, ensureTrusted, onGrant, onLaunch, onBack }: {
  choice: BriefLaunchChoice; grant: TeamGrant; ensureTrusted: () => Promise<boolean>;
  onGrant: (value: TeamGrant) => void; onLaunch: (value: TeamGrant) => Promise<unknown>; onBack: () => void;
}) {
  const { projectId, draft, nextVersion, exportPath } = choice;
  const [saved, setSaved] = useState<Version>();
  useUnsavedBrief(!saved);
  const client = useQueryClient();
  const heading = useRef<HTMLHeadingElement>(null);
  const feedback = useRef<HTMLDivElement>(null);
  useEffect(() => { heading.current?.focus(); }, []);
  const decision = useMutation({
    mutationFn: async (action: "save" | "without") => {
      if (!await ensureTrusted()) return;
      let selected = { ...grant, brief_version: 0 };
      if (action === "save") {
        const value = saved ?? await post<Version>(`/api/projects/${projectId}/brief`, {
          version: draft.base, expected_next_version: nextVersion, fields: draft.draft,
          references: draft.references.map(r => r.reference_id), export_path: exportPath,
        });
        setSaved(value);
        selected = { ...selected, brief_version: value.version,
          run_mode: grant.run_mode === "build" && value.submitted.run_mode === "build" ? "build" : "prepare" };
        onGrant(selected);
        if (JSON.stringify(readBriefDraft(projectId)) === JSON.stringify(draft)) keepBriefDraft(projectId, null);
        window.dispatchEvent(new CustomEvent<SavedBriefEvent>("acc-brief-saved", {
          detail: { projectId, draft, saved: value },
        }));
        await client.invalidateQueries({ queryKey: ["project-brief", projectId] });
      }
      await onLaunch(selected);
    },
  });
  useEffect(() => { if (decision.error) feedback.current?.focus(); }, [decision.error]);
  return <section className="min-w-0 space-y-4" aria-labelledby="ns-brief-choice">
    <h3 ref={heading} id="ns-brief-choice" tabIndex={-1} className="font-medium focus-visible:outline-2 focus-visible:outline-accent">
      {saved ? `Brief version ${saved.version} is saved and selected for this team.`
        : `Save this brief as version ${nextVersion} and pin it to the team?`}
    </h3>
    <p className="text-[13px] text-muted">The team cannot read an unsaved draft. Saving keeps earlier versions and includes the draft's private references.</p>
    <p className="whitespace-pre-wrap break-words text-[13px]">{draft.draft.product}</p>
    <p className="text-[13px] text-muted">Save and launch pins version {saved?.version ?? nextVersion}.
      {" "}Team run mode: {grant.run_mode === "build" && draft.draft.run_mode === "build" ? "Build" : "Prepare"}.
      {" "}Launch without the brief pins none and keeps any unsaved draft in this tab. Back returns to your choices.</p>
    <div ref={feedback} tabIndex={-1} className="focus-visible:outline-2 focus-visible:outline-accent">
      <ErrorNote error={decision.error} title={saved ? `Brief version ${saved.version} is saved, but the team couldn't launch. Retry the launch or go Back.` : "Couldn't save and launch. Your draft is kept."} />
    </div>
    <div className="flex flex-wrap justify-end gap-2">
      <Button variant="ghost" disabled={decision.isPending} onClick={onBack}>Back</Button>
      <Button disabled={decision.isPending} onClick={() => decision.mutate("without")}>Launch without the brief</Button>
      <Button variant="primary" disabled={decision.isPending} onClick={() => decision.mutate("save")}>
        {decision.isPending ? "Working…" : saved ? "Launch with saved brief" : "Save and launch"}
      </Button>
    </div>
  </section>;
}
