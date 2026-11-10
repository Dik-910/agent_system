## dump_ctl

### JsonConfig
```Json
{
  "briefInfo": "dump ctl",
  "description": "Writes out messages for CTL to the specified file",
  "isAtLeastOneArg": true,
}
```
### Argument

**file**

```Json
{
  "token": [],
  "description": "[file]  (The output file name)",
  "argType": "string",
  "isOptional": false,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specifies the file name for the output. The option must be specified.
- Valid Value: string
- Default Value: N/A
- Rule: None

- Syntax
```
<*file*>
file := <string>
```
### Description

Write the ctl information in the current DB database to the specified file name. And you must specify a file name.

### Example

Scenario : The following example shows the basic usage of this directive.

```
dump_ctl "./outputs/out.ctl"
```

### See Also

[load_ctl](#load_ctl)
