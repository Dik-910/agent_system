## load_ctl

### JsonConfig
```Json
{
  "briefInfo": "load ctl",
  "description": "Reads in one or more files in ctl format.",
  "isAtLeastOneArg": true,
}
```
### Argument

**-module**

```Json
{
  "token": ["-module"],
  "description": "[-module]  (Specifies the name of the module to which to attach the stil.)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [1,1],
  "orderNum": 0
}
```


- Description:
  Specifies the name of the module to which to attach the test model. This is
  meaningful only for CTL test models. By default, the model is attached to the
  current module.
- Valid Value: string
- Default Value: N/A
- Rule: None

- Syntax
```
[-module]
```
**files**

```Json
{
  "token": [],
  "description": "[files]  (Specifies one or more ctl files)",
  "argType": "string",
  "isOptional": false,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specifies one or more ctl file that need to be loaded.
- Valid Value: string
- Default Value: N/A
- Rule: None

- Syntax
```
<*files*>
files := <string>
```
### Description

Load one or more ctl file.

### Example

Scenario : The following example shows the basic usage of this directive.

```
load_ctl -module cur_module ./TEST_1.ctl
```

### See Also

[dump_ctl](#dump_ctl)
