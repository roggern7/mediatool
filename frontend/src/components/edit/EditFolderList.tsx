import { ChevronRight, Folder } from "lucide-react";
import type { EditFolder } from "../../api/types";
import { folderSubtitle, folderTitle, versionCountText } from "../../lib/editFolders";
import { formatBytes } from "../../lib/format";

/** Lista de pastas da página Editar (uma por importação/grupo, por dia de envio e "Outros vídeos"). */
export function EditFolderList({ folders, onOpen }: { folders: EditFolder[]; onOpen: (folder: EditFolder) => void }) {
  return (
    <ul className="edit-folders" aria-label="Pastas de vídeos">
      {folders.map((folder) => (
        <li key={folder.key}>
          <button type="button" className="edit-folder" onClick={() => onOpen(folder)}>
            <Folder size={16} strokeWidth={1.5} aria-hidden="true" />
            <span className="edit-folder-text">
              <span className="edit-folder-title">{folderTitle(folder)}</span>
              <span className="muted small">
                {folderSubtitle(folder)}
                {folder.rendition_count > 0 ? ` · ${versionCountText(folder.rendition_count)}` : ""}
                {` · ${formatBytes(folder.total_bytes)}`}
              </span>
            </span>
            <ChevronRight size={14} strokeWidth={1.75} aria-hidden="true" />
          </button>
        </li>
      ))}
    </ul>
  );
}
