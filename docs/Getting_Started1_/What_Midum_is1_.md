# What is Midum?

**Midum is a desktop AI runtime designed to turn AI conversations into capable, persistent workflows.**

Most AI applications are built around a simple interaction: you send a prompt, receive a response, and start again. Midum takes a different approach. It provides the tools, integrations, execution environment, and workspace that allow AI models to do more than generate text.

Rather than being tied to a single model or provider, Midum acts as an interface between AI models and the work you want them to accomplish. You choose the model, configure its capabilities, and give it access to the tools and resources needed for your tasks.

## More than a chatbot

Midum brings several capabilities together in one desktop application:

- **AI agents:** Let models work through multi-step tasks using tools, context, and delegated agents.
- **Tools and integrations:** Extend capabilities through built-in tools and external MCP integrations.
- **Knowledge Bases:** Give models access to organized information and reference material.
- **Skills:** Define reusable instructions and specialized capabilities for different tasks.
- **Flows:** Build structured workflows by connecting individual operations.
- **MidumDocs:** Create and manage documents within an integrated document workspace.
- **Workbench:** Access an integrated development environment for coding and working on projects.
- **Surfaces:** Turn model-generated output into interactive interfaces, visualizations, and desktop widgets.
- **Voice:** Interact through voice input and output.
- **Persistent workspaces:** Keep conversations, resources, and workflows organized beyond individual prompts.

These capabilities are designed to work together. A model can use a knowledge base, invoke tools, follow a workflow, create an artifact, and present its results through an interactive interface—all within the same environment.

## Model-agnostic by design

Midum is not built around one particular AI model.

You can configure supported model providers, use your own API keys, switch between models, configure fallback providers, or connect compatible local models.

This separation is intentional: **the model provides the reasoning; Midum provides the environment in which that reasoning can be put to work.**

Different models have different strengths, limitations, costs, and availability. Midum gives you control over which models you use instead of locking your entire workflow to one provider.

## Built around user control

Midum is designed to give users control over their AI environment.

- Configure which tools and integrations are available.
- Review and manage permissions for actions that require approval.
- Choose your model providers and credentials.
- Organize your own knowledge, skills, and workflows.
- Use local models where supported.
- Keep application data and configuration under your control.

Midum does not operate its own model-inference service. When you use an external provider, its services and policies apply. Some features may also connect to external services when required.

## Privacy-focused by design

Midum is designed to keep users in control of their data.

**Your workspace is yours.** Midum does not require an account or a login to use its core functionality. Your conversations, configuration, knowledge bases, and other application data are stored locally on your device rather than being maintained in a Midum-operated cloud account.

Midum also does not rely on a central Midum backend to process your conversations or run its AI runtime.

This means:

- **Local data storage:** Your application data is maintained on your own device.
- **No mandatory account:** You can use Midum without creating a Midum account.
- **Bring your own provider:** You choose which AI providers and models you connect to.
- **Control over integrations:** External tools and MCP integrations are configured by you, with permissions governing access to capabilities.
- **No Midum-operated conversation service:** Midum does not route your conversations through its own centralized inference infrastructure.

### Understanding external connections

Privacy also depends on how you configure and use Midum.

When you connect an external AI provider, the information sent to that provider is subject to its privacy policy and data-handling practices. Similarly, MCP integrations and other external services may transmit information required to perform their functions.

Some Midum features also require internet access, such as external integrations, updates, and online document compilation.

**Local storage does not automatically mean local processing.** If you use a cloud-based model or service, relevant information may leave your device to be processed by that service.

Midum aims to make these boundaries clear and give users control over the services they choose to use.

## Powerful software shouldn't need to be a luxury

Midum is built around the idea that capable software should be accessible.

Its core runtime is designed to provide substantial functionality without artificially restricting essential capabilities behind feature tiers. Users can bring their own model providers, use compatible local models, and build workflows around their own requirements.

External model usage, infrastructure, and third-party services may have their own costs or limitations.

## Who is Midum for?

Midum is intended for people who want more control over how they use AI:

- **Developers** who want AI-assisted coding, tool execution, project workflows, and an integrated workbench.
- **Researchers and students** who need to organize information, work with documents, and automate repetitive tasks.
- **Creators** who want to generate content, build interactive interfaces, and work with multimedia tools.
- **Power users** who want to customize their AI environment rather than rely on a fixed chatbot experience.

You do not need to use every feature. Midum is modular enough to support simple conversations as well as more elaborate workflows.

## The idea behind Midum

Midum is built on a simple principle:

**An AI model should not be limited to the boundaries of a chat window.**

It should be able to work with tools, interact with its environment, use information, follow structured processes, and produce useful results.

Midum provides the environment for that to happen.

**One app. Infinite possibilities. Unusually capable.**