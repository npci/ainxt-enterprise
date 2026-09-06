# AI-UI Mock Server

This is a mock server implementation for the AI-UI React application that simulates all backend API endpoints, allowing you to run the frontend application locally without requiring the actual backend services.

## Features

- Complete mock implementation of all API endpoints used by the AI-UI application
- Simulated authentication flow
- Mock data for chats, users, agents, workflows, and more
- Streaming responses for chat interactions
- Support for file uploads and document generation
- Realistic dummy data for all endpoints

## Getting Started

### Prerequisites

- Node.js (v14 or higher)
- npm (v6 or higher)

### Installation

1. Navigate to the ai-ui directory:
```bash
cd ai-ui
```

2. Install dependencies:
```bash
npm install
```

### Running the Mock Server

There are several ways to run the mock server:

#### Option 1: Run the mock server separately
```bash
npm run mock-server
```

This starts the mock server on port 8000.

#### Option 2: Run both mock server and frontend together
```bash
npm run start-mock
```

This runs both the mock server and the React development server simultaneously.

#### Option 3: Run just the frontend (if mock server is already running)
```bash
npm run dev
```

The frontend will automatically proxy API requests to the mock server at `http://localhost:8000`.

## API Endpoints

The mock server implements all the API endpoints used by the AI-UI application:

### Authentication
- `POST /ainxt/v1/api/auth/login` - Login endpoint
- `GET /ainxt/v1/api/auth/me` - Get current user info
- `POST /ainxt/v1/api/auth/logout` - Logout endpoint

### Chat Management
- `GET /ainxt/v1/api/chats` - Get all chats
- `GET /ainxt/v1/api/chats/:chatId/messages` - Get chat messages
- `DELETE /ainxt/v1/api/chats/:chatId` - Delete chat
- `PATCH /ainxt/v1/api/chats/:chatId/pin` - Pin/unpin chat
- `PATCH /ainxt/v1/api/chats/:chatId/title` - Update chat title

### User & Configuration
- `GET /ainxt/v1/api/budget/me` - Get user budget
- `GET /ainxt/v1/api/all-models` - Get all available models
- `GET /ainxt/v1/api/model-governance/my-models` - Get user's allowed models
- `GET /ainxt/v1/api/inbox/unread-count` - Get unread inbox count
- `GET /ainxt/v1/api/sdlc/stats` - Get SDLC statistics

### Application Data
- `GET /ainxt/v1/api/agents` - Get agents
- `GET /ainxt/v1/api/workflows` - Get workflows
- `GET /ainxt/v1/api/skills` - Get skills
- `GET /ainxt/v1/api/projects` - Get projects
- `GET /ainxt/v1/api/threads` - Get threads
- `GET /ainxt/v1/api/knowledge` - Get knowledge base
- `GET /ainxt/v1/api/products` - Get products
- `GET /ainxt/v1/api/codebase` - Get codebase
- `GET /ainxt/v1/api/marketplace` - Get MCP marketplace
- `GET /ainxt/v1/api/budget` - Get budget information
- `GET /ainxt/v1/api/level-overrides` - Get level overrides

### AI Interaction
- `POST /ainxt/v1/api/ask` - Ask question (streaming)
- `POST /ainxt/v1/api/ask/image` - Ask question with image (streaming)
- `POST /ainxt/v1/api/chat/upload` - Upload files
- `POST /ainxt/v1/api/docs/generate` - Generate documents
- `POST /ainxt/v1/api/chat/messages/:msgId/feedback` - Submit feedback
- `GET /ainxt/v1/api/agents/:agentName/run` - Run agent
- `GET /ainxt/v1/api/voice/tts` - Text-to-speech

## Usage

1. Start the mock server:
   ```bash
   npm run start-mock
   ```

2. Open your browser to `http://localhost:5173`

3. Login with any email and password (credentials are not validated in the mock)

4. You can now use all UI features without needing the actual backend services

## Customization

To customize the mock data:
1. Edit the mock data variables at the top of `mock-server.cjs`
2. Modify the route handlers to return different responses
3. Add new endpoints as needed

## License

This project is licensed under the MIT License.
