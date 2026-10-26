/** @type {import('tailwindcss').Config} */
// SUDO Consultants visual identity (sudo-brand): Navy #204d9b leads, Bright Blue #189cd9 accents,
// #F5F7FA page background, #333333 text, Poppins headings and Noto Sans text. Everything else is
// neutral slate; red / orange / amber / green / sky are reserved for ESI severity and for state
// (critical, warning, done), never for decoration.
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['"Noto Sans"', 'ui-sans-serif', 'system-ui', '-apple-system', '"Segoe UI"', 'Arial', 'sans-serif'],
        display: ['Poppins', '"Noto Sans"', 'ui-sans-serif', 'system-ui', 'Arial', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
      },
      colors: {
        // SUDO Navy (primary): 600 is the brand colour.
        brand: {
          50: '#eff3fa',
          100: '#dbe4f3',
          200: '#b9cae8',
          300: '#8eaad9',
          400: '#5d82c2',
          500: '#3a64ad',
          600: '#204d9b',
          700: '#1b4287',
          800: '#183871',
          900: '#152f5d',
          950: '#0e1f3e',
        },
        // SUDO Bright Blue (secondary): 500 is the brand colour. Highlights, focus, links.
        accent: {
          50: '#eaf6fc',
          100: '#cdeaf8',
          200: '#a2d8f2',
          300: '#6dc2ea',
          400: '#3aade1',
          500: '#189cd9',
          600: '#1382b8',
          700: '#116895',
          800: '#12577a',
          900: '#134966',
        },
        // Deep navy for the video room (dark surfaces).
        navy: {
          700: '#1e2f52',
          800: '#152340',
          900: '#0f1a30',
          950: '#0a1222',
        },
        canvas: '#F5F7FA',
        ink: '#333333',
        line: '#E0E0E0',
      },
      backgroundImage: {
        'brand-gradient': 'linear-gradient(135deg, #204d9b 0%, #189cd9 100%)',
      },
      boxShadow: {
        card: '0 1px 2px 0 rgb(21 47 93 / 0.04), 0 1px 3px 0 rgb(21 47 93 / 0.06)',
      },
    },
  },
  plugins: [],
}
