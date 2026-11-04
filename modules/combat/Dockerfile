FROM golang:1.27.1 AS build
ENV GOPROXY=https://goproxy.cn,direct
WORKDIR /src
# The shared protocol library is a sibling module wired in through a replace
# directive, so the root build context must copy it before go mod download.
COPY lib/wow ./lib/wow
COPY modules/combat/go.mod modules/combat/go.sum ./modules/combat/
WORKDIR /src/modules/combat
RUN go mod download
COPY modules/combat ./
RUN CGO_ENABLED=0 go build -trimpath -o /module ./cmd

FROM scratch
COPY --from=build /module /module
ENTRYPOINT ["/module"]
