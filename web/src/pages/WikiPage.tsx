import { useParams } from "react-router-dom";
import { Page, PageHeader } from "../components/ui";
import { Wiki } from "../components/Wiki";
import { useProjects } from "../lib/queries";
import { workspaceName } from "../lib/format";

export function ProjectWiki() {
  const { id = "", "*": page = "" } = useParams();
  const project = useProjects().data?.find((p) => String(p.id) === id);
  return (
    <Page wide>
      <PageHeader title={project ? `${project.name} wiki` : "Wiki"}
        description={<>User guide and internal documentation, written by the project's agents in <span className="font-mono">docs/wiki/</span> (Azure DevOps wiki layout, so it can also be published there).</>} />
      <Wiki kind="project" scopeKey={id} base={`/projects/${id}/wiki`} page={page} />
    </Page>
  );
}

export function WorkspaceWiki() {
  const { folder = "", "*": page = "" } = useParams();
  return (
    <Page wide>
      <PageHeader title={`${workspaceName(folder)} wiki`}
        description={<>Notes and findings for this workspace, in <span className="font-mono">docs/wiki/</span>. Promoting the workspace keeps them.</>} />
      <Wiki kind="workspace" scopeKey={folder} base={`/workspaces/${folder}/wiki`} page={page}
        empty={<>No wiki yet. Workspace sessions add one in <span className="font-mono text-fg">docs/wiki/</span> when the research is worth keeping.</>} />
    </Page>
  );
}

/** The Guide: Rynhult's own wiki (every feature, with screenshots), for everyone who can sign in. */
export function GuideWiki() {
  const { "*": page = "" } = useParams();
  return (
    <Page wide>
      <PageHeader title="Guide"
        description={<>How Rynhult works: every feature, with screenshots. The same pages as <span className="font-mono">docs/wiki/</span> in Rynhult's repository.</>} />
      <Wiki kind="guide" scopeKey="acc" base="/guide" page={page} />
    </Page>
  );
}
