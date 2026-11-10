## present_design

### JsonConfig
```Json
{
  "briefInfo": "present design",
  "description": "Sets or returns the present design",
  "isAtLeastOneArg": false,
}
```
### Argument

**design**

```Json
{
  "token": [],
  "description": "[design_name]  (Specifies the working or focal design. If you omit the design_name option, the present_design command returns a cluster containing the present design)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specifies the working or focal design. If you omit the design_name option, the present_design command returns a cluster containing the present design.
- Valid Value: string
- Default Value: N/A
- Rule: None

- Syntax
```
[<*design_name*>]
design_name := <string>
```
### Description

This command sets or returns the working design, which is the focus of many commands.

### Example

Scenario : The following example uses the present_design command to show the current context and to change the context from one design to another.

```
present_design
present_design top
present_design
```

### See Also
