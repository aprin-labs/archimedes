import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import { defineConfig, globalIgnores } from 'eslint/config'

// react-hooks v7.x added aspirational rules that require either a data-
// fetching library (react-query/SWR) or structural refactor of every
// fetch-on-mount pattern in the app. They are real React best practice
// but the cost-of-fix this weekend is days of refactoring + introducing
// a new dependency surface. Disabled with explanation; revisit when we
// move to react-query post-hackathon.
//   - react-hooks/set-state-in-effect: flags setState() inside useEffect
//     bodies even when the effect is the SSR-equivalent boundary; our
//     fetch-then-setState pattern across Portfolio/Generate/Explore
//     would need to become useQuery() hooks.
//   - react-hooks/refs: flags reads of `ref.current` during render.
//     CorpusGraph reads container dimensions to size the force-graph,
//     which is a legitimate pattern with ResizeObserver as the proper
//     replacement.
//
// react-refresh/only-export-components is a Vite HMR quality-of-life
// rule, not correctness; we export helper functions next to components
// in several files (e.g. shared formatters). Disabled.
const DISABLED_ASPIRATIONAL = {
  'react-hooks/set-state-in-effect': 'off',
  'react-hooks/refs': 'off',
  'react-refresh/only-export-components': 'off',
}

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{js,jsx}'],
    extends: [
      js.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    rules: {
      ...DISABLED_ASPIRATIONAL,
      // Allow `_foo` to mark intentional unused vars / args — common
      // pattern for destructuring or callback signatures we can't
      // change. Without this override the default flags `_metaErr`,
      // `_e`, etc. which are clearly intentional.
      'no-unused-vars': [
        'error',
        {
          argsIgnorePattern: '^_',
          varsIgnorePattern: '^_',
          caughtErrorsIgnorePattern: '^_',
        },
      ],
    },
  },
  // postcss.config.js runs in Node, not the browser.
  {
    files: ['postcss.config.js', 'vite.config.js', 'eslint.config.js'],
    languageOptions: { globals: { ...globals.node } },
  },
  // `npm test` is `node --test`, so the suite executes in Node and may use
  // Node globals (#1602 needs `process.env.TZ` to re-home the local calendar
  // and prove the chart's date labels do not move with the viewer's timezone).
  // Browser globals stay in scope too — these files also assert on UI source.
  {
    files: ['test/**/*.js', 'scripts/**/*.mjs'],
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
  },
])
