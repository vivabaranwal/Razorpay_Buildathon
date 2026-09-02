/** @type {import('tailwindcss').Config} */
export default {
  // Class strategy rather than media: the toggle must be able to override the
  // OS preference in both directions, which a media-only setup cannot do.
  darkMode: ["class", '[data-theme="dark"]'],
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      // Every colour resolves to a CSS variable, so light and dark are two
      // value sets in index.css rather than two sets of utility classes.
      colors: {
        bg: "var(--bg)",
        surface: "var(--surface)",
        "surface-raised": "var(--surface-raised)",
        border: "var(--border)",
        "border-strong": "var(--border-strong)",

        primary: "var(--text-primary)",
        secondary: "var(--text-secondary)",
        muted: "var(--text-muted)",

        accent: "var(--accent)",
        "accent-hover": "var(--accent-hover)",
        "accent-ink": "var(--accent-ink)",
        "accent-text": "var(--accent-text)",

        success: "var(--success)",
        warning: "var(--warning)",
        danger: "var(--danger)",
        "success-text": "var(--success-text)",
        "warning-text": "var(--warning-text)",
        "danger-text": "var(--danger-text)",
      },
      fontFamily: {
        display: ["Fraunces", "Georgia", "serif"],
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
      },
      borderRadius: {
        card: "16px",
        control: "10px",
      },
      spacing: {
        card: "28px",
      },
    },
  },
  plugins: [],
};
