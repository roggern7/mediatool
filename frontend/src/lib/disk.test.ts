import { describe, expect, it } from "vitest";
import { diskShortage, diskText } from "./disk";

const GB = 1024 ** 3;

describe("espaço em disco", () => {
  it("detecta falta de espaço pelos números ou pelo backend", () => {
    expect(diskShortage({ free_bytes: 3 * GB, required_bytes: 6 * GB })).toBe(true);
    expect(diskShortage({ free_bytes: 50 * GB, required_bytes: 6 * GB })).toBe(false);
    expect(diskShortage({ free_bytes: 50 * GB, required_bytes: 6 * GB, disk_full: true })).toBe(true);
    expect(diskShortage({ free_bytes: 50 * GB, required_bytes: 6 * GB, ok: false })).toBe(true);
    expect(diskShortage({ free_bytes: -1, required_bytes: 6 * GB })).toBe(false);
    expect(diskShortage(undefined)).toBe(false);
  });
  it("texto com espaço livre e mínimo", () => {
    expect(diskText({ free_bytes: 12 * GB, required_bytes: 6 * GB })).toBe(
      "Espaço livre: 12,00 GB (mínimo para continuar baixando: 6,00 GB)",
    );
    expect(diskText({ free_bytes: -1, required_bytes: 0 })).toBe("Não foi possível medir o espaço livre");
  });
});
