import os
import asyncio
import json
import logging
import pprint
from exceptions import ConfigurationError, ConnectionError, ToolError

from dotenv import load_dotenv
from dataclasses import dataclass
from typing import Optional, Union, Any, Dict, List
from contextlib import AsyncExitStack
from colorama import init, Fore, Style
init(autoreset=True)  # Initialize colorama with autoreset=True

from pydantic import BaseModel
from pydantic_ai import Agent, RunContext 
from pydantic_ai.tools import Tool, ToolDefinition

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from httpx import AsyncClient
from supabase import Client
from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIModel

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

def initialize_client_and_model() -> tuple[AsyncOpenAI, OpenAIModel, str]:
    """
    Load environment variables, resolve provider-specific configuration,
    and return the client and model.

    Returns:
        tuple[AsyncOpenAI, OpenAIModel, str]: A tuple containing the client, model, and language model.

    Raises:
        ConfigurationError: If any required environment variable is missing.
    """

    load_dotenv()  
    selected = os.getenv("SELECTED")

    required_env_vars = {var: os.getenv(f"{selected}_{var}") for var in ["URL", "API_KEY", "MODEL"]}
    
    missing_vars = [var for var, value in required_env_vars.items() if not value]
    if missing_vars:
        raise ConfigurationError(f"Missing environment variables: {', '.join(missing_vars)}")
    
    base_url, api_key, language_model = required_env_vars["URL"], required_env_vars["API_KEY"], required_env_vars["MODEL"]

    client = AsyncOpenAI( 
        base_url=base_url,
        api_key=api_key)
    
    model = OpenAIModel(
        language_model,
        base_url=base_url,
        api_key=api_key)
    
    return client, model, language_model
    
try:
    client, model, language_model = initialize_client_and_model()
    logger.info("Client and model initialized successfully.")
except ConfigurationError as e:
    # Explain what happens with the raise here, what is executed next ?
    logger.error(f"Configuration error: {e}")
    raise
    
except Exception as e:
    logger.exception("Unexpected error during client and model initialization")
    raise

# System prompt that guides the LLM's behavior and capabilities
# This helps the model understand its role and available tools
SYSTEM_PROMPT = """You are a helpful assistant capable of accessing external functions and engaging in casual chat. Use the responses from these function calls to provide accurate and informative answers. The answers should be natural and hide the fact that you are using tools to access real-time information. Guide the user about available tools and their capabilities. Always utilize tools to access real-time information when required. Engage in a friendly manner to enhance the chat experience.
 
# Tools
 
{tools}
 
# Notes 
 
- Ensure responses are based on the latest information available from function calls.
- Maintain an engaging, supportive, and friendly tone throughout the dialogue.
- Always highlight the potential of available tools to assist users comprehensively."""
 
@dataclass
class Deps:
    client: AsyncClient
    supabase: Client
    session_id: str

class MCPClient:
    """
    A client class for interacting with the MCP (Model Control Protocol) server.
    This class manages the connection and communication with the tools through MCP.
    """
    def __init__(self):
        # Initialize sessions and agents dictionaries
        self.sessions: Dict[str, ClientSession] = {}  # Dictionary to store {server_name: session}
        self.agents: Dict[str, Agent] = {}  # Dictionary to store {server_name: agent}
        self.exit_stack = AsyncExitStack()
        self.available_tools = []
        self.tools = {}
        self.connected = False
        self.config_file = 'mcp_config.json'
        self.dynamic_tools: List[Tool] = []  # List to store dynamic pydantic tools

    async def connect_to_server(self) -> None:
        """
        Connect to the MCP server using the configuration file.
        
        Raises:
            ConfigurationError: If the configuration file is missing or invalid.
            ConnectionError: If unable to connect to the MCP server.
        """
        if self.connected:
            logging.info("Already connected to servers.")
            return

        logger.info(f"Loading configuration from {self.config_file}.")
        try:
            with open(self.config_file) as f:
                config = json.load(f)
        except FileNotFoundError:
            raise ConfigurationError(f"{self.config_file} file not found.")
        except json.JSONDecodeError:
            raise ConfigurationError(f"{self.config_file} is not a valid JSON file.")
        
        logger.debug("Available servers in config: %s", list(config['mcpServers'].keys()))
        
        # Connect only to enabled servers in config
        for server_name, server_config in config['mcpServers'].items():
            logger.info(f"Processing server configuration for {server_name}.")
            logger.debug(f"Server configuration details: %s", json.dumps(server_config, indent=2))
            if server_config.get("enable", False):
                logger.debug(f"Attempting to load {server_name} server config.")
                
                server_params = StdioServerParameters(
                    command=server_config['command'],
                    args=server_config['args'],
                    env=server_config.get('env'),
                )
                logger.info("Created server parameters: command=%s, args=%s, env=%s",
                              server_params.command, server_params.args, server_params.env)
               
                try:
                    # Create and store session with server name as key
                    stdio_transport = await self.exit_stack.enter_async_context(stdio_client(server_params))
                    stdio, write = stdio_transport
                    session = await self.exit_stack.enter_async_context(ClientSession(stdio, write))
                    await session.initialize()
                    self.sessions[server_name] = session
                    
                    # Create and store an Agent for this server
                    server_agent: Agent = Agent(
                        model,
                        system_prompt=(
                            f"You are an AI assistant that helps interact with the {server_name} server. "
                            "You will use the available tools to process requests and provide responses."
                            "Make sure to always give feedback to the user after you have called the tool, especially when the tool does not generate any message itself."
                        )
                    )
                    self.agents[server_name] = server_agent
                    
                    # List available tools for this server
                    response = await session.list_tools()
                    server_tools = [{
                        "name": f"{server_name}__{tool.name}",
                        "description": tool.description,
                        "input_schema": tool.inputSchema
                    } for tool in response.tools]
                except Exception as e:
                    raise ConnectionError(f"Failed to connect to MCP server {server_name}: {str(e)}")
                
                # Add server's tools to overall available tools
                self.available_tools.extend(server_tools)

                # Create corresponding dynamic pydantic tools
                # if pydantic-ai provides fix for OpenAI this can be used
                # now no dynalic tools are used
                for tool in response.tools:
                    
                    # Long descriptions beyond 1023 are not supported with OpenAI,
                    # so replacing with a local file description optimized for use if it exists.
                    file_name = f"./mcp-tool-description-overrides/{server_name}__{tool.name}"

                    if os.path.exists(file_name):
                        try:
                            with open(file_name, 'r') as f:
                                file_content = f.read()
                            tool.description = file_content
                        except Exception as e:
                            logging.error(f"An error occurred while reading the file: {e}")
                            raise
                        finally: 
                            f.close
                    else:
                        logger.debug(f"File '{file_name}' not found. Using default description.")

                    # Create corresponding dynamic pydantic tools
                    dynamic_tool = self.create_dynamic_tool(tool, server_name, server_agent)
                    self.tools[tool.name] = {
                        "name": tool.name,
                        "callable": self.call_tool(f"{server_name}__{tool.name}"),
                        "schema": {
                            "type": "function",
                            "function": {
                                "name": tool.name,
                                "description": tool.description,
                                "parameters": tool.inputSchema,
                            },
                        },
                    }
                    logger.debug(f"Added tool: {tool.name}")
                
                logger.info(f"Connected to server {server_name} with tools: {', '.join(tool['name'] for tool in server_tools)}")

                self.connected = True
            else:
                logging.info(f"Server {server_name} is disabled. Skipping connection.")
        logging.info("Done connecting to servers.")

    async def add_mcp_configuration(self, query: str) -> Optional[str]:
        """
        Add a new MCP server configuration if the query starts with 'mcpServer'.
        The query should be in the format:
        {"server_name": {"command": "command", "args": ["arg1", "arg2"], "env": null}}

        Args:
            query (str): The configuration query in JSON format.

        Returns:
            Optional[str]: Success message or error message if the operation fails.
        """
        braces_warning = ""
        try:
            config_str = query  # Define config_str from the query
            # Check for mismatched curly braces and attempt to fix
            open_braces = config_str.count('{')
            close_braces = config_str.count('}')
            if open_braces > close_braces:
                config_str += '}' * (open_braces - close_braces)
                braces_warning = "Added missing closing brace(s) to the configuration."
            elif close_braces > open_braces:
                config_str = '{' * (close_braces - open_braces) + config_str
                braces_warning = "Added missing opening brace(s) to the configuration."
            config_str = query
            new_config = json.loads(config_str)
            logging.debug("New configuration to add:", json.dumps(new_config, indent=2))

            # Validate the new configuration
            if not isinstance(new_config, dict):
                return "Error: Configuration must be a JSON object."

            # The server name is the key in the new_config dictionary
            server_name = next(iter(new_config), None)
            if not server_name:
                return "Error: Server name is required as the key in the configuration."

            server_config = new_config[server_name]

            # Validate the server configuration
            if not isinstance(server_config, dict):
                return f"Error: Configuration for server '{server_name}' must be a JSON object."

            if "command" not in server_config or "args" not in server_config:
                return f"Error: 'command' and 'args' are required for server '{server_name}'."

            # Load the existing config
            try:
                with open(self.config_file, "r") as f:
                    config = json.load(f)
            except FileNotFoundError:
                return f"Error: {self.config_file} file not found."
            except json.JSONDecodeError:
                return f"Error: {self.config_file} is not a valid JSON file."

            # Check if the server name already exists
            if server_name in config.get("mcpServers", {}):
                return f"Error: Server '{server_name}' already exists in the configuration."

            # Add the new server configuration with default enabled status
            if "mcpServers" not in config:
                config["mcpServers"] = {}

            config["mcpServers"][server_name] = {
                "command": server_config["command"],
                "args": server_config["args"],
                "env": server_config.get("env"),  # Optional field
                "enable": server_config.get("enable", True)  # Default to True if not specified
            }

            # Save the updated config back to the file
            with open(self.config_file, "w") as f:
                json.dump(config, f, indent=2)

            # Connect to the new server
            await self.connect_to_server_with_config(server_name, config["mcpServers"][server_name])

            return f"Successfully added and connected to server '{server_name}'."

        except json.JSONDecodeError:
            return "Error: Invalid JSON format in the query."
        except Exception as e:
            raise f"Error adding MCP configuration: {str(e)}"


    async def drop_mcp_server(self, server_name: str) -> str:
        """
        Remove an MCP server from the configuration and disconnect it.

        Args:
            server_name (str): The name of the server to remove.

        Returns:
            str: Success message or error message if the operation fails.
        """
        try:
            with open(self.config_file, "r") as f:
                config = json.load(f)

            if server_name not in config.get("mcpServers", {}):
                return f"Error: Server '{server_name}' does not exist in the configuration."

            # Remove the server from the configuration
            del config["mcpServers"][server_name]

            # Save the updated config back to the file
            with open(self.config_file, "w") as f:
                json.dump(config, f, indent=2)

            # Disconnect the server if it is connected
            if server_name in self.sessions:
                del self.sessions[server_name]
                del self.agents[server_name]

            return f"Successfully removed and disconnected server '{server_name}'."

        except FileNotFoundError:
            return f"Error: {self.config_file} file not found."
        except json.JSONDecodeError:
            return f"Error: {self.config_file} is not a valid JSON file."
        except Exception as e:
            return f"Error removing MCP server: {str(e)}"
         
    async def connect_to_server_with_config(self, server_name: str, server_config: dict) -> None:
        """
        Connect to a server using the provided configuration.

        Args:
            server_name (str): The name of the server.
            server_config (dict): The server configuration dictionary.
        """
        server_params = StdioServerParameters(
            command=server_config['command'],
            args=server_config['args'],
            env=None
        )
        stdio_transport = await self.exit_stack.enter_async_context(stdio_client(server_params))
        stdio, write = stdio_transport
        session = await self.exit_stack.enter_async_context(ClientSession(stdio, write))
        await session.initialize()
        self.sessions[server_name] = session

        response = await session.list_tools()
        server_tools = [{
            "name": f"{server_name}__{tool.name}",
            "description": tool.description,
            "input_schema": tool.inputSchema
        } for tool in response.tools]

        self.available_tools.extend(server_tools)
        return None

    async def list_mcp_servers(self) -> str:
        """
        List all MCP servers in the configuration.

        Returns:
            str: A formatted string listing enabled and disabled servers.
        """
        try:
            with open(self.config_file, "r") as f:
                config = json.load(f)

            enabled_servers = [
                server_name for server_name, server_config in config.get("mcpServers", {}).items()
                if server_config.get("enable", True)
            ]
            disabled_servers = [
                server_name for server_name, server_config in config.get("mcpServers", {}).items()
                if not server_config.get("enable", True)
            ]
            suggestion = "\n - Use /functions &lt;server_name&gt; to list functions provided by a specific server."
            newline = '\n'
            return f"**Enabled servers:**{newline}1. {newline}1. ".join(enabled_servers) + f"{newline}{newline}Disabled servers: {', '.join(disabled_servers)}{newline}Next command suggestion: {suggestion}"
        except FileNotFoundError:
            return f"Error: {self.config_file} file not found."
        except json.JSONDecodeError:
            return f"Error: {self.config_file} is not a valid JSON file."

    async def list_server_functions(self, server_name: str) -> str:
        """
        List all functions provided by a specific MCP server.

        Args:
            server_name (str): The name of the server.

        Returns:
            str: A formatted string listing the functions or an error message.
        """
        if server_name not in self.sessions:
            return f"Error: Server '{server_name}' is not connected."
        try:
            response = await self.sessions[server_name].list_tools()
            functions = []
            for tool in response.tools:
                parameters = tool.inputSchema.get('properties', {})
                functions.append({
                    "function name": tool.name,
                    "parameters": parameters
                })
            return f"Functions for server '{server_name}':\n" + json_to_markdown(functions)

        except Exception as e:
            return f"Error listing functions for server '{server_name}': {str(e)}"

    async def cleanup(self) -> None:
        """
        Clean up resources by closing sessions and clearing tool lists.
        """
        logging.debug("Cleaning up resources...")
        await self.exit_stack.aclose()
        self.sessions.clear()
        self.available_tools.clear()
        self.connected = False
        logging.info("Cleanup completed.")

    async def toggle_server_status(self, server_names: List[str], enable: bool) -> str:
        """
        Enable or disable specific MCP servers.

        Args:
            server_names (List[str]): List of server names to toggle.
            enable (bool): True to enable, False to disable.

        Returns:
            str: A message indicating the result of the operation.
        """
        try:
            with open(self.config_file, "r") as f:
                config = json.load(f)

            results = []
            for server_name in server_names:
                if server_name not in config.get("mcpServers", {}):
                    results.append(f"Error: Server '{server_name}' does not exist in the configuration.")
                    continue

                # Update the enabled status
                config["mcpServers"][server_name]["enable"] = enable
                status = "enabled" if enable else "disabled"
                results.append(f"Successfully {status} server '{server_name}'.")

            # Save the updated config back to the file
            with open(self.config_file, "w") as f:
                json.dump(config, f, indent=2)

            return "\n".join(results)

        except FileNotFoundError:
            return "Error: mcp_config.json file not found."
        except json.JSONDecodeError:
            return "Error: mcp_config.json is not a valid JSON file."
        except Exception as e:
            return f"Error toggling server status: {str(e)}"
        
    async def cleanup(self):
        """Clean up resources."""
        logging.debug("Cleaning up resources...")
        await self.exit_stack.aclose()
        self.sessions.clear()
        self.available_tools.clear()
        self.connected = False
        logging.info("Cleanup completed.")
    
    async def get_available_tools(self) -> List[Any]:
        """
        Retrieve a list of available tools from the MCP server.
        Simplify the schema for each tool to make it compatible with the OpenAI API.

        Returns:
            List[Any]: A list of available tools with simplified schemas.
        """
        if not self.sessions:
            raise RuntimeError("Not connected to MCP server")
    
        def simplify_schema(schema):
            """
            Simplifies a JSON schema by removing unsupported constructs like 'allOf', 'oneOf', etc.,
            and preserving the core structure and properties. Needed for pandoc to work with the LLM.

            Args:
                schema (dict): The original JSON schema.

            Returns:
                dict: A simplified JSON schema.
            """
            # Create a new schema with only the basic structure
            simplified_schema = {
                "type": "object",
                "properties": schema.get("properties", {}),
                "required": schema.get("required", []),
                "additionalProperties": schema.get("additionalProperties", False)
            }

            # Remove unsupported constructs like 'allOf', 'oneOf', 'anyOf', 'not', 'enum' at the top level
            for key in ["allOf", "oneOf", "anyOf", "not", "enum"]:
                if key in simplified_schema:
                    del simplified_schema[key]

            return simplified_schema

        return  {
            tool['name']: {
                "name": tool['name'],
                "callable": self.call_tool(
                    tool['name']
                ),  # returns a callable function for the rpc call
                "schema": {
                    "type": "function",
                    "function": {
                        "name": tool['name'],
                        "description": tool['description'][:1023],
                        "parameters": simplify_schema(tool['input_schema'])
                    },
                },
            }
            for tool in self.available_tools
            if tool['name']
            != "xxx"  # Excludes xxx tool as it has an incorrect schema
        }
        
    def call_tool(self, server__tool_name: str) -> Any:
        """
        Create a callable function for a specific tool.
        This allows us to execute functions through the MCP server.

        Args:
            server__tool_name (str): The name of the tool to create a callable for.

        Returns:
            Any: A callable async function that executes the specified tool.
        """
        server_name, tool_name = server__tool_name.split("__")  

        if not server_name in self.sessions:
            raise RuntimeError("Not connected to MCP server")
 
        async def callable(*args, **kwargs):
            try:
                response = await asyncio.wait_for(
                    self.sessions[server_name].call_tool(tool_name, arguments=kwargs),
                    timeout=10.0  # Set a timeout
                )
                return response.content[0].text if response.content else None
            except asyncio.TimeoutError:
                # pandoc docker will not return timely respons
                logging.debug("Timeout while calling MCP server")
                return None
            except Exception as e:
                #ignore for now, many mcp servers not production ready
                logging.error(f"Error calling MCP server: {e}")
                return None
 
        return callable
    
    def create_dynamic_tool(self, tool, server_name: str, server_agent: Agent) -> Tool:
        """
        Create a dynamic tool for a given server and tool.

        Args:
            tool: The tool object.
            server_name (str): The name of the server.
            server_agent (Agent): The agent associated with the server.

        Returns:
            Tool: A dynamic tool object.
        """
        async def prepare_tool(
            ctx: RunContext[str], 
            tool_def: ToolDefinition,
            tool_name: str = tool.name,
            server: str = server_name
        ) -> Union[ToolDefinition, None]:
            # Customize tool definition based on server context
            tool_def.name = f"{server}__{tool_name}"
            tool_def.description = f"Tool from {server} server: {tool.description}"
            logging.info(tool_def.description)
            return tool_def

        async def tool_func(ctx: RunContext[Any], str_arg) -> str:
            agent_response = await server_agent.run_sync(str_arg)
            logging.debug(f"Server agent response: {agent_response}")
            logging.info(f"Tool {tool.name} called with {str_arg}. Agent response: {agent_response}")
            return f"Tool {tool.name} called with {str_arg}. Agent response: {agent_response}"

        return Tool(
            tool_func,
            prepare=prepare_tool,
            name=f"{server_name}__{tool.name}",
            description=tool.description
        )
    async def handle_slash_commands(self, query: str) -> str:
        """
        Handle slash commands for adding MCP servers and listing available functions.

        Args:
            query (str): The command query.

        Returns:
            str: The result of the command execution.
        """
        try:
            command, *args = query.split()
            if command == "/addMcpServer":
                result = await self.add_mcp_configuration(" ".join(args))
            elif command == "/list":
                result = await self.list_mcp_servers()
            elif command == "/enable" and args:
                result = await self.toggle_server_status(args, True)  # Pass list of server names
            elif command == "/disable" and args:
                result = await self.toggle_server_status(args, False)  # Pass list of server names
            elif command == "/functions" and args:
                result = await self.list_server_functions(args[0])
            elif command == "/dropMcpServer" and args:
                result = await self.drop_mcp_server(args[0])
            else:
                result = "Error: Invalid command or missing arguments."
        except Exception as e:
            logging.error(f"Error handling slash commands: {e}")
            raise

        return result
    
async def agent_loop(query: str, tools: dict, messages: List[dict] = None, deps: Deps = None):
    """
    Main interaction loop that processes user queries using the LLM and available tools.
 
    This function:
    1. Sends the user query to the LLM with context about available tools
    2. Processes the LLM's response, including any tool calls
    3. Returns the final response to the user
 
    Args:
        query: User's input question or command
        tools: Dictionary of available tools and their schemas
        messages: List of messages to pass to the LLM, defaults to None
    """
 
    messages = (
        [
            {
                "role": "system",
                "content": SYSTEM_PROMPT.format(
                    tools="\n- ".join(
                        [
                            f"{t['name']}: {t['schema']['function']['description']}"
                            for t in tools.values()
                        ]
                    )
                ),  # Creates System prompt based on available MCP server tools
            },
        ]
        if messages is None
        else messages  # reuse existing messages if provided
    )
    # add user query to the messages list
    messages.append({"role": "user", "content": query})
    pprint.pprint(messages)

    # Query LLM with the system prompt, user query, and available tools
    first_response = await client.chat.completions.create(
        model=language_model,
        messages=messages,
        tools=([t["schema"] for t in tools.values()] if len(tools) > 0 else None),
        max_tokens=4096,
        temperature=0,
    )
    # detect how the LLM call was completed:
    # tool_calls: if the LLM used a tool
    # stop: If the LLM generated a general response, e.g. "Hello, how can I help you today?"
    stop_reason = (
        "tool_calls"
        if first_response.choices[0].message.tool_calls is not None
        else first_response.choices[0].finish_reason
    )
 
    if stop_reason == "tool_calls":
        # Extract tool use details from response
        for tool_call in first_response.choices[0].message.tool_calls:
            arguments = (
                json.loads(tool_call.function.arguments)
                if isinstance(tool_call.function.arguments, str)
                else tool_call.function.arguments
            )
            # Call the tool with the arguments using our callable initialized in the tools dict
            logging.debug(tool_call.function.name)
            tool_result = await tools[tool_call.function.name]["callable"](**arguments)
            if tool_result is None:
                tool_result = f"{tool_call.function.name}"
            #logging.debug("tool result begin")
            #pprint.pprint(tool_result)
            #logging.debug("tool result end")

            # Add tool call to messages with an id
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": json.dumps(arguments)
                    }
                }]
            })
            
            # Add the tool result to the messages list
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_call.function.name,
                    "content": json.dumps(tool_result),
                }
            )
            pprint.pprint(messages)

        # Query LLM with the user query and the tool results
        new_response = await client.chat.completions.create(
            model=language_model,
            messages=messages,
        )
 
    elif stop_reason == "stop":
        # If the LLM stopped on its own, use the first response
        new_response = first_response
    else:
        raise ValueError(f"Unknown stop reason: {stop_reason}")
    
    # Add the LLM response to the messages list
    messages.append(
        {"role": "assistant", "content": new_response.choices[0].message.content}
    )

    # Return the LLM response and messages
    return new_response.choices[0].message.content, messages

def json_to_markdown(data, indent=0):
    markdown = ""
    prefix = "  " * indent  # Indentation for nested structures

    if isinstance(data, dict):
        for key, value in data.items():
            markdown += f"{prefix}- **{key}**: "
            if isinstance(value, (dict, list)):
                markdown += "\n" + json_to_markdown(value, indent + 1)
            else:
                markdown += f"{value}\n"
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                markdown += f"{prefix}- \n{json_to_markdown(item, indent + 1)}"
            else:
                markdown += f"{prefix}- {item}\n"
    else:
        markdown += f"{prefix}- {data}\n"

    return markdown

async def main():
    """
    Main function that sets up the MCP server, initializes tools, and runs the interactive loop.
    """
    mcp_client = MCPClient()
    await mcp_client.connect_to_server()

    tools = await mcp_client.get_available_tools()
    
    # Start interactive prompt loop for user queries
    messages = None
    while True:
        try:
            # Get user input and check for exit commands
            user_input = input("\nEnter your prompt (or 'quit' to exit): ")
            if user_input.lower() in ["quit", "exit", "q"]:
                break
            if user_input.startswith("/"):
                response = await mcp_client.handle_slash_commands(user_input)
            else:
                # Process the prompt and run agent loop
                response, messages = await agent_loop(user_input, tools, messages)
            logging.debug("Response:", response)
            # logging.debug("Messages:", messages)
        except KeyboardInterrupt:
            logging.debug("Exiting...")
            break
        except Exception as e:
            logging.error(f"Error occurred: {e}")
 
    await mcp_client.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
