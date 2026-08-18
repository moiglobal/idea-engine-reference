# About me and this project

I am a long-term, value-oriented investor in the Buffett and Munger tradition.
This project is an engine that surfaces one investment idea on most working
days that fits my criteria, and emails me a one-page memo on it. It runs every
weekday and sends when it has something new; on a thin day it tells me it has
nothing rather than repeating a name or lowering its standard.

# What I care about in a business

- Durable competitive advantage and high, consistent returns on capital.
- A reasonable price relative to free cash flow and earnings power.
- Honest, capable management and a strong balance sheet.
- I will consider smaller companies and non-US companies, not just large US names.

# How I want memos written

- Plain English. No marketing language. Active voice. No em-dashes.
- Specific numbers, with the figure and where it came from.
- Always state the bear case and what would have to be true.
- Never imply this is a recommendation. It is an idea to investigate.

# How I want you to work

- Compute ratios from raw financial statements, never from precomputed ratio fields.
- Currency is handled once, on fetch: no calculation may combine two figures
  unless both have been converted to US dollars. A missing exchange rate
  excludes the company and is reported; it never falls back to comparing
  unconverted numbers.
- When calling SEC EDGAR, always send the User-Agent header from
  SEC_USER_AGENT, and never exceed 10 requests per second. For non-US
  companies that file with the SEC, look for the ifrs-full taxonomy rather
  than us-gaap.
- Show me the evidence, not the assurance. Give me the number, the count, or
  the raw response, not "it worked".
- Tell me when data looks wrong or thin. Say "data not available" rather than guess.
- Ask before making judgment calls. Propose options when there is a real choice.
- If this file grows past about 60 lines, tell me and suggest which lines to
  cut or move into a skill.
