import { Link, useParams, useSearchParams } from "react-router-dom";
import { Files } from "../components/Files";
import { Page, PageHeader, TabPanel, Tabs } from "../components/ui";
import { workspaceName } from "../lib/format";

export function WorkspaceFiles() {
  const { folder = "" } = useParams();
  const [search] = useSearchParams();
  const session = search.get("session");
  return <Page wide><PageHeader title={workspaceName(folder)} description="Files in this workspace's folder" actions={<Link to="/workspaces" className="text-accent hover:underline underline underline-offset-2">Back to Workspaces</Link>} />
    <Tabs id="workspace-tabs" tabs={[{ id: "files", label: "Files" }]} value="files" onChange={() => {}} />
    <TabPanel id="workspace-tabs" value="files">
      {session ? <Files key={session} kind="workspace" scopeKey={session} /> : <p className="text-muted">Open Files from the workspace card to choose its folder.</p>}
    </TabPanel>
  </Page>;
}
