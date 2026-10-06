import { api } from "../api/client";
import { diskShortage, diskText } from "../lib/disk";
import { useQuery } from "../lib/useQuery";

/** Faixa global quando falta espaço em disco (pausa disk_full): downloads aguardam até liberar espaço. */
export function DiskBanner() {
  const settings = useQuery("settings-disk", () => api.getSettings(), 30_000);
  const disk = settings.data?.disk;
  if (!disk || !diskShortage(disk)) return null;
  return (
    <div className="alert warn" role="alert" style={{ marginBottom: 14 }}>
      <span className="grow">
        Pouco espaço livre em disco: os downloads estão parados. {diskText(disk)}. Libere espaço e o Media Tool
        continua sozinho (ele confere a cada minuto).
      </span>
    </div>
  );
}
