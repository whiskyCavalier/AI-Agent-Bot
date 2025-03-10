
# Avoid flake8 and pylint errors for long lines
# flake8: noqa
# pylint: disable=line-too-long

DEFAULT_PROMPT_ENHANCEMENT_TEXT = """
Improve the given initial prompt to make it clearer, more effective, and aligned with the task objectives and expectations.

# Steps

1. Carefully review the initial prompt provided.
2. Identify unclear instructions, missing details, or any ambiguity that could affect the model’s performance.
3. Add specific guidelines, necessary context, or well-defined examples if needed.
4. Make sure the prompt provides a clear and straightforward reasoning process before concluding the answer. 
5. Ensure the expected output is explicitly defined, including format, structure, and requirements.
6. Avoid unnecessary complexity—focus on simplicity, clarity, and effectiveness.

# Output Format

An enhanced version of the prompt with:
- Clarity in language and expectations
- Structured reasoning before conclusions
- Defined output format for consistency.

# Example

**Initial Prompt (Input)**:  
"Explain why a tomato is a fruit, and then list some related fruits."

**Enhanced Prompt (Output)**:  
"Explain step-by-step why a tomato is scientifically classified as a fruit. Start by describing the botanical characteristics that belong to fruits. After explaining, provide a list of other fruits that share similar characteristics as a tomato, such as being soft and containing seeds."

### Notes:
- When enhancing the prompt, ensure reasoning precedes any conclusion or answer.
- Always define how the output should be structured (e.g., format length or elements).
"""
