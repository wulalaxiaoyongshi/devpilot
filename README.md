这是一个coding harness agent，主要做的是分析、修复、新增代码，利用git mcp server拉取一个仓库的issues，通过问题去完成修改或新增代码，再去验证、测试代码是否正确，之后publish-pr，等待人工审核后合并到main
