// Suggestions only: the vault still validates reserved names on the server.
export const ENV_VARIABLE_PATTERN = /^[A-Z_][A-Z0-9_]{0,63}$/;

interface VariableGroup {
  provider: string;
  variables: readonly { name: string; description: string }[];
}

export const ENV_VARIABLE_GROUPS: readonly VariableGroup[] = [
  { provider: "OpenAI", variables: [
    { name: "OPENAI_API_KEY", description: "OpenAI API access" },
  ] },
  { provider: "Anthropic", variables: [
    { name: "ANTHROPIC_API_KEY", description: "Anthropic API access" },
  ] },
  { provider: "Google AI", variables: [
    { name: "GEMINI_API_KEY", description: "Gemini API access" },
    { name: "GOOGLE_API_KEY", description: "Google AI API access" },
  ] },
  { provider: "Azure OpenAI", variables: [
    { name: "AZURE_OPENAI_API_KEY", description: "Azure OpenAI API access" },
    { name: "AZURE_OPENAI_ENDPOINT", description: "Azure OpenAI resource URL" },
  ] },
  { provider: "OpenRouter", variables: [
    { name: "OPENROUTER_API_KEY", description: "OpenRouter model access" },
  ] },
  { provider: "Mistral", variables: [
    { name: "MISTRAL_API_KEY", description: "Mistral API access" },
  ] },
  { provider: "Groq", variables: [
    { name: "GROQ_API_KEY", description: "Groq inference API access" },
  ] },
  { provider: "xAI", variables: [
    { name: "XAI_API_KEY", description: "xAI model access" },
  ] },
  { provider: "DeepSeek", variables: [
    { name: "DEEPSEEK_API_KEY", description: "DeepSeek API access" },
  ] },
  { provider: "Hugging Face", variables: [
    { name: "HF_TOKEN", description: "Hugging Face models and Hub access" },
  ] },
  { provider: "GitHub", variables: [
    { name: "GH_TOKEN", description: "GitHub CLI authentication" },
    { name: "GITHUB_TOKEN", description: "GitHub API and CLI authentication" },
  ] },
  { provider: "GitLab", variables: [
    { name: "GITLAB_TOKEN", description: "GitLab API or issue-source token" },
  ] },
  { provider: "Linear", variables: [
    { name: "LINEAR_API_KEY", description: "Linear API or issue-source key" },
  ] },
  { provider: "AWS", variables: [
    { name: "AWS_ACCESS_KEY_ID", description: "AWS access key identifier" },
    { name: "AWS_SECRET_ACCESS_KEY", description: "AWS secret access key" },
    { name: "AWS_SESSION_TOKEN", description: "AWS temporary credential token" },
  ] },
  { provider: "Azure", variables: [
    { name: "AZURE_CLIENT_ID", description: "Azure service principal client ID" },
    { name: "AZURE_CLIENT_SECRET", description: "Azure service principal secret" },
    { name: "AZURE_TENANT_ID", description: "Azure directory tenant ID" },
  ] },
  { provider: "Google Cloud", variables: [
    { name: "GOOGLE_APPLICATION_CREDENTIALS", description: "Credential file path, not JSON contents" },
  ] },
  { provider: "Cloudflare", variables: [
    { name: "CLOUDFLARE_API_TOKEN", description: "Cloudflare API access" },
  ] },
  { provider: "npm", variables: [
    { name: "NPM_TOKEN", description: "npm registry authentication" },
  ] },
  { provider: "PyPI", variables: [
    { name: "PYPI_TOKEN", description: "PyPI token for tools configured to use it" },
    { name: "TWINE_PASSWORD", description: "PyPI upload token for Twine" },
  ] },
  { provider: "Application", variables: [
    { name: "DATABASE_URL", description: "Application database connection URL" },
  ] },
  { provider: "Stripe", variables: [
    { name: "STRIPE_SECRET_KEY", description: "Stripe server-side API access" },
  ] },
  { provider: "Sentry", variables: [
    { name: "SENTRY_DSN", description: "Sentry error reporting destination" },
  ] },
];

export const ENV_VARIABLES = ENV_VARIABLE_GROUPS.flatMap((group) => group.variables);
