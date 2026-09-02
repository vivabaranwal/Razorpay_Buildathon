/**
 * "The Itch" - the narrative behind SettleTrace.
 *
 * Built entirely on the dashboard's tokens so it reads as the same product
 * rather than a marketing page bolted on beside it. The only additions are
 * editorial: grain, chapter markers, a pull-quote, and a wider type scale
 * than the app itself uses.
 *
 * Layout rhythm comes from width, not decoration: prose is locked to a narrow
 * measure and only the headline, the evidence row, and the proof numbers are
 * allowed to break out of it.
 */

import type { ReactNode } from "react";
import { ArrowLeft, ArrowRight } from "lucide-react";
import { useCountUp, useReveal, useScrollProgress } from "../hooks/useReveal";

/** Prose measure. Everything else in a section is deliberately wider. */
const COLUMN = "mx-auto w-full max-w-[664px]";

function Section({
  chapter,
  title,
  wash = false,
  children,
}: {
  chapter: string;
  title?: string;
  wash?: boolean;
  children: ReactNode;
}) {
  const [ref, visible] = useReveal<HTMLElement>();

  return (
    <section
      ref={ref}
      className={`reveal relative px-6 py-[clamp(80px,13vh,150px)] ${
        visible ? "is-visible" : ""
      } ${wash ? "section-wash" : ""}`}
    >
      <div className="mx-auto w-full max-w-[1080px]">
        <div className={`${COLUMN} mb-10`}>
          <span className="chapter">{chapter}</span>
          {title && (
            <h2
              className="font-display mt-5 text-[clamp(30px,4vw,44px)] font-medium leading-[1.08]"
              style={{ fontVariationSettings: '"opsz" 96', letterSpacing: "-0.02em" }}
            >
              {title}
            </h2>
          )}
        </div>
        {children}
      </div>
    </section>
  );
}

function Prose({ children }: { children: ReactNode }) {
  return (
    <div className={`${COLUMN} space-y-6`}>
      <div className="space-y-6 text-[16.5px] leading-[1.72] text-secondary">
        {children}
      </div>
    </div>
  );
}

/** A pinned exhibit. The tilt alternates so the row never reads as uniform. */
function Exhibit({
  source,
  quote,
  detail,
  index,
}: {
  source: string;
  quote: string;
  detail: string;
  index: number;
}) {
  // Staggered by 110ms per card: the sequence is what sells "considered".
  const [ref, visible] = useReveal<HTMLDivElement>(index * 110);
  const tilt = [-1.4, 1.1, -0.8][index % 3];

  return (
    <div
      ref={ref}
      className={`reveal exhibit ${visible ? "is-visible" : ""}`}
      style={{ transform: visible ? `rotate(${tilt}deg)` : undefined }}
    >
      <span className="exhibit-tag">{source}</span>
      <p className="font-display text-[19px] leading-[1.45] text-primary">
        “{quote}”
      </p>
      <p className="mt-4 text-[13.5px] leading-relaxed text-muted">{detail}</p>
    </div>
  );
}

/** One ruled-out alternative. Reads as eliminating a suspect, not a feature row. */
function RuledOut({ name, verdict }: { name: string; verdict: string }) {
  return (
    <p>
      <span className="font-medium text-primary">{name}</span>
      <span className="mx-2.5 text-muted">—</span>
      {verdict}
    </p>
  );
}

const PRINCIPLES = [
  "The AI never decides. It only explains.",
  "Every automatic correction is written down.",
  "Nothing is ever silently dropped.",
];

/**
 * One closing promise.
 *
 * Its own component rather than an inline map callback: useReveal is a hook,
 * and calling a hook inside a callback breaks the rule that hook order must be
 * identical on every render.
 */
function Principle({ line, index }: { line: string; index: number }) {
  const [ref, visible] = useReveal<HTMLDivElement>(index * 140);

  return (
    <div ref={ref} className={`reveal ${visible ? "is-visible" : ""}`}>
      <p
        className="font-display mx-auto max-w-[18ch] text-center font-medium leading-[1.15]"
        style={{
          fontSize: "clamp(30px, 4.6vw, 50px)",
          letterSpacing: "-0.025em",
          fontVariationSettings: '"opsz" 120',
        }}
      >
        {line}
      </p>
    </div>
  );
}

function ProofNumber({
  value,
  suffix,
  label,
  decimals = 0,
}: {
  value: number;
  suffix: string;
  label: string;
  decimals?: number;
}) {
  const [ref, visible] = useReveal<HTMLDivElement>();
  const counted = useCountUp(value, visible);

  return (
    <div ref={ref} className="text-center">
      <div className="proof-glow">
        <div
          className="font-display tabular font-semibold leading-none"
          style={{
            fontSize: "clamp(52px, 8vw, 104px)",
            fontVariationSettings: '"opsz" 144',
            letterSpacing: "-0.03em",
          }}
        >
          {counted.toFixed(decimals)}
          {suffix}
        </div>
      </div>
      <div className="label mt-6">{label}</div>
    </div>
  );
}

export function TheItch({ onNavigate }: { onNavigate: (to: string) => void }) {
  const progress = useScrollProgress();
  const [hookRef, hookVisible] = useReveal<HTMLElement>();

  return (
    <div className="grain relative">
      {/* Wayfinding, not decoration: a hairline that says how much is left. */}
      <div
        className="fixed left-0 top-0 z-50 h-[3px] origin-left"
        style={{
          width: "100%",
          background: "var(--accent)",
          transform: `scaleX(${progress})`,
          transition: "transform 80ms linear",
        }}
      />

      <div className="relative z-[2]">
        {/* 01 - Opening hook. The largest type anywhere in the product. */}
        <section
          ref={hookRef}
          className={`reveal px-6 pb-[clamp(60px,9vh,110px)] pt-[clamp(90px,15vh,180px)] ${
            hookVisible ? "is-visible" : ""
          }`}
        >
          <div className="mx-auto w-full max-w-[1080px]">
            <span className="chapter">01 — The itch</span>
            <h1
              className="font-display mt-8 max-w-[15ch] font-medium"
              style={{
                fontSize: "clamp(46px, 8.5vw, 92px)",
                lineHeight: 0.97,
                letterSpacing: "-0.035em",
                fontVariationSettings: '"opsz" 144',
              }}
            >
              Razorpay&rsquo;s own blog admits it. We built the fix.
            </h1>
            <div className={`${COLUMN} mx-0 mt-10`}>
              <p className="text-[17px] leading-[1.7] text-secondary">
                Merchants get a single number in their bank account and no way
                to check it. Orders sit on &ldquo;pending&rdquo; for days
                because one webhook never arrived. Both problems are
                documented, both are unsolved, and neither needs new
                infrastructure to fix.
              </p>
            </div>
          </div>
        </section>

        {/* 02 - The problem, evidenced. */}
        <Section chapter="02 — The problem" title="Two gaps, both on the record." wash>
          <Prose>
            <p>
              A settlement arrives as one net figure. Which transactions made it
              up, what was deducted as fees, what GST was charged on those fees
              — none of it is itemised. To verify a payout you would have to
              rebuild it by hand from a spreadsheet, so almost nobody does.
            </p>
            <p>
              Separately, payment status depends on a webhook arriving. When one
              is delayed, duplicated, or never sent, the merchant&rsquo;s store
              is simply wrong about whether it was paid — and stays wrong until
              a human notices.
            </p>
          </Prose>

          {/* Breaks wider than the prose column, on purpose. */}
          <div className="mt-16 grid gap-7 md:grid-cols-3">
            <Exhibit
              index={0}
              source="razorpay.com/blog"
              quote="Net-only payouts with no transaction linkage."
              detail="Razorpay's own 2026 settlement-transparency post, naming the gap in its own product."
            />
            <Exhibit
              index={1}
              source="github — razorpay-woocommerce #571"
              quote="Five hours between capture and webhook delivery."
              detail="Filed against Razorpay's own plugin. The order stays pending for the whole window."
            />
            <Exhibit
              index={2}
              source="shopify community"
              quote="Store frozen — payment still shows processing."
              detail="Eight days and counting, with no mechanism for the merchant to resolve it themselves."
            />
          </div>
        </Section>

        {/* 03 - The investigation. */}
        <Section
          chapter="03 — The investigation"
          title="First we checked whether someone had already fixed it."
        >
          <Prose>
            <p>
              Building something that already exists is the most expensive
              mistake available, so the first work was elimination, not code.
              Four candidates looked like they might already cover this. None
              of them did.
            </p>
            <RuledOut
              name="Svix and Hookdeck"
              verdict="guarantee delivery of a webhook that was sent. They cannot recover an event the source never emitted, which is the actual failure here."
            />
            <RuledOut
              name="Ethoca, Verifi, Chargeflow"
              verdict="warn about card-network disputes. They run on Visa and Mastercard rails and do not see UPI at all — and UPI is most of Razorpay's volume."
            />
            <RuledOut
              name="Razorpay's Single Reconciliation View"
              verdict="shows the settlement report. It is the net-only view itself, not a way to interrogate it."
            />
            <RuledOut
              name="Community reconciliation scripts"
              verdict="match records for bookkeeping after the fact. They assume both systems already agree, which is precisely the assumption that fails."
            />
          </Prose>

          <div className={COLUMN}>
            <blockquote className="pull-quote">
              Every tool in the space either guarantees delivery of an event
              that was sent, or reconciles books that already agree. Nothing
              catches the payout that silently doesn&rsquo;t add up.
            </blockquote>
          </div>

          <Prose>
            <p>
              That was the gap worth building into: Razorpay-aware,
              transaction-level, and self-correcting rather than merely
              observational.
            </p>
          </Prose>
        </Section>

        {/* 04 - The solution. */}
        <Section chapter="04 — The build" title="Two modules. No magic." wash>
          <Prose>
            <p>
              <span className="font-medium text-primary">
                The Settlement Linkage Auditor
              </span>{" "}
              takes a payout and rebuilds it transaction by transaction. For
              each one it recalculates what the fee, the tax on that fee, and
              any withheld reserve should have been under the merchant&rsquo;s
              own rate card, then compares that to what was actually deducted.
              Anything that disagrees becomes a flagged item with the exact
              rupee difference attached.
            </p>
            <p>
              <span className="font-medium text-primary">
                The Payment State Reconciler
              </span>{" "}
              watches for orders still unresolved past the point where that
              payment method normally settles. When it finds one, it asks
              Razorpay directly what the true status is, corrects the
              merchant&rsquo;s record, and writes down what it changed and why.
              It runs on its own schedule — nobody has to press anything.
            </p>
          </Prose>
        </Section>

        {/* 05 - The proof. The page's climax. */}
        <Section chapter="05 — The proof" title="Measured, not asserted.">
          <div className="mt-20 grid gap-20 sm:grid-cols-3 sm:gap-10">
            <ProofNumber value={96.4} decimals={1} suffix="%" label="Auto-matched" />
            <ProofNumber value={100} suffix="%" label="Precision" />
            <ProofNumber value={100} suffix="%" label="Recall" />
          </div>
          <div className={`${COLUMN} mt-20`}>
            <p className="text-center text-[15px] leading-[1.7] text-secondary">
              Measured against known planted defects, not asserted. The test
              batch carries deliberate errors whose locations are known in
              advance, so precision and recall are checked against ground truth
              — which is the only way an accuracy number means anything.
            </p>
          </div>
        </Section>

        {/* 06 - The principle. Each promise gets its own moment. */}
        <Section chapter="06 — The principle" wash>
          <div className="space-y-24">
            {PRINCIPLES.map((line, i) => (
              <Principle key={line} line={line} index={i} />
            ))}
          </div>
        </Section>

        {/* Close. */}
        <footer
          className="relative border-t px-6 py-20"
          style={{ borderColor: "var(--border)" }}
        >
          <div className="mx-auto flex w-full max-w-[1080px] flex-col gap-8">
            <button
              onClick={() => onNavigate("/")}
              className="group inline-flex w-fit items-center gap-3"
            >
              <ArrowLeft
                size={18}
                strokeWidth={1.5}
                style={{ color: "var(--accent-text)" }}
              />
              <span
                className="font-display text-[22px] font-medium"
                style={{ color: "var(--accent-text)" }}
              >
                Open the live dashboard
              </span>
              <ArrowRight
                size={18}
                strokeWidth={1.5}
                className="opacity-0 transition-opacity group-hover:opacity-100"
                style={{ color: "var(--accent-text)" }}
              />
            </button>

            <p className="max-w-[52ch] text-[13.5px] leading-relaxed text-muted">
              A submission to the Razorpay AI Buildathon 2026, AI Finance
              Controller track. Built by{" "}
              <a
                href="https://vivabaranwal.vercel.app/"
                target="_blank"
                rel="noopener noreferrer"
                className="underline decoration-1 underline-offset-4"
                style={{ color: "var(--accent-text)" }}
              >
                Viva Baranwal
              </a>
              .
            </p>
          </div>
        </footer>
      </div>
    </div>
  );
}
