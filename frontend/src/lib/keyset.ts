// Paginação keyset (before_id / next_before_id) com recuo para offset quando o backend é antigo.

/** Posição de uma página: primeira (null), keyset (before_id) ou offset (backend sem next_before_id). */
export type PageCursor = null | { before_id: number } | { offset: number };

/** Pilha de posições visitadas; o topo é a página atual. `scope` identifica os filtros. */
export interface Pager {
  scope: string;
  stack: PageCursor[];
}

export interface KeysetPage {
  items: unknown[];
  total: number;
  next_before_id?: number | null;
}

export const FIRST_PAGE: PageCursor[] = [null];

/** Pilha válida para os filtros atuais; filtros diferentes voltam para a primeira página. */
export function stackFor(pager: Pager, scope: string): PageCursor[] {
  return pager.scope === scope && pager.stack.length > 0 ? pager.stack : FIRST_PAGE;
}

export function currentCursor(stack: PageCursor[]): PageCursor {
  return stack[stack.length - 1] ?? null;
}

/** Parâmetros de consulta para a posição. */
export function cursorQuery(cursor: PageCursor): { before_id?: number; offset?: number } {
  if (cursor === null) return {};
  return "before_id" in cursor ? { before_id: cursor.before_id } : { offset: cursor.offset };
}

/** Posição da próxima página, ou null se esta é a última. */
export function nextCursor(page: KeysetPage, stack: PageCursor[], pageSize: number): PageCursor {
  if (page.next_before_id === undefined) {
    // Backend antigo: sem cursor, segue por offset.
    const offset = stack.length * pageSize;
    return offset < page.total ? { offset } : null;
  }
  return page.next_before_id === null ? null : { before_id: page.next_before_id };
}

export function pushPage(stack: PageCursor[], cursor: PageCursor): PageCursor[] {
  return cursor === null ? stack : [...stack, cursor];
}

export function popPage(stack: PageCursor[]): PageCursor[] {
  return stack.length > 1 ? stack.slice(0, -1) : FIRST_PAGE;
}

/** Índice (base 0) do primeiro item da página atual, aproximado quando há itens novos. */
export function pageStart(stack: PageCursor[], pageSize: number): number {
  return (stack.length - 1) * pageSize;
}

/** Página vazia fora da primeira com itens no total: os itens saíram; volta para a primeira. */
export function shouldResetToFirst(page: KeysetPage, stack: PageCursor[]): boolean {
  return stack.length > 1 && page.items.length === 0 && page.total > 0;
}
