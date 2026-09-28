import parser from '@typescript-eslint/parser'
import plugin from '@typescript-eslint/eslint-plugin'

export default [{
  files: ['src/**/*.{ts,tsx}'],
  languageOptions: { parser, parserOptions: { ecmaFeatures: { jsx: true } } },
  plugins: { '@typescript-eslint': plugin },
  rules: { ...plugin.configs.recommended.rules },
}]
