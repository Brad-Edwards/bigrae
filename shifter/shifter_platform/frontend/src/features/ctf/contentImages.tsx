/**
 * First-party copies of external images in organizer content (#2090, ADR-036).
 *
 * The portal enforces `img-src 'self' data:`, so the browser cannot load an
 * image from another host. The server copies external images from organizer
 * content into platform storage and returns a map from each source URL to its
 * portal-origin copy. Renderers resolve every image source through this module:
 * a mapped external image uses its copy, an external image without a ready copy
 * becomes a link, and same-origin or relative images stay as they are.
 */
import { createContext, useContext, useMemo, type ReactNode } from "react";

export type ContentImageMap = ReadonlyMap<string, string>;

export type ResolvedContentImage = { kind: "image"; src: string } | { kind: "link"; href: string };

const EMPTY_MAP: ContentImageMap = new Map();
const ContentImageContext = createContext<ContentImageMap>(EMPTY_MAP);
const EXTERNAL = /^https?:\/\//i;
const HAS_SCHEME = /^[a-z][a-z0-9+.-]*:/i;

/** Provide the image map for the content rendered below; entries without a ready copy are ignored. */
export function ContentImageProvider({
  images,
  children,
}: Readonly<{ images?: readonly { source_url: string; url: string | null }[]; children: ReactNode }>) {
  const map = useMemo<ContentImageMap>(
    () => new Map((images ?? []).flatMap((image) => (image.url ? [[image.source_url, image.url] as const] : []))),
    [images],
  );
  return <ContentImageContext.Provider value={map}>{children}</ContentImageContext.Provider>;
}

export function useContentImageMap(): ContentImageMap {
  return useContext(ContentImageContext);
}

/** Resolve one image source under the enforced CSP, or `null` when it cannot be shown. */
export function resolveContentImage(map: ContentImageMap, src: string | undefined): ResolvedContentImage | null {
  if (!src) return null;
  if (EXTERNAL.test(src)) {
    const copy = map.get(src);
    return copy ? { kind: "image", src: copy } : { kind: "link", href: src };
  }
  if (src.startsWith("//") || HAS_SCHEME.test(src)) return null;
  return { kind: "image", src };
}
