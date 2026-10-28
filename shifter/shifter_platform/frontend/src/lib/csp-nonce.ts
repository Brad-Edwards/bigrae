/**
 * Per-response CSP style nonce for script-created `<style>` elements (ADR-036,
 * #2090).
 *
 * The portal enforces `style-src 'self' 'nonce-…'`. Some SPA libraries (the
 * dialog scroll lock and the xterm renderer) create `<style>` elements at run
 * time, and neither offers a nonce option. The Django SPA host publishes the
 * response nonce in `<meta property="csp-nonce" nonce="…">`; this module stamps
 * it on `<style>` elements that script creates through `document.createElement`.
 *
 * Markup that the HTML parser creates (for example injected `innerHTML`) never
 * passes through this factory, so injected style elements and style attributes
 * stay blocked. Scripts never receive the nonce.
 */

const NONCE_META_SELECTOR = 'meta[property="csp-nonce"]';

/** Return the nonce the SPA host published, or `undefined` when there is none. */
export function readCspNonce(doc: Document = document): string | undefined {
  const meta = doc.querySelector<HTMLMetaElement>(NONCE_META_SELECTOR);
  // Browsers hide the nonce attribute after load; the property keeps the value.
  const nonce = meta?.nonce || meta?.getAttribute("nonce");
  return nonce || undefined;
}

/**
 * Stamp the published nonce on script-created `<style>` elements of `doc`.
 * Returns a function that restores the original factory.
 */
export function installStyleNonce(doc: Document = document): () => void {
  const nonce = readCspNonce(doc);
  if (!nonce) {
    return () => {};
  }
  const original = doc.createElement;
  const stamped = function (this: Document, tagName: string, options?: ElementCreationOptions): HTMLElement {
    const element = original.call(this, tagName, options);
    if (element.localName === "style") {
      element.setAttribute("nonce", nonce);
    }
    return element;
  };
  doc.createElement = stamped as Document["createElement"];
  return () => {
    if (doc.createElement === stamped) {
      // Removing the own property restores the prototype factory.
      delete (doc as { createElement?: unknown }).createElement;
    }
  };
}
