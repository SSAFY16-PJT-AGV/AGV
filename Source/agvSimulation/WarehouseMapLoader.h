#pragma once
#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "WarehouseMapLoader.generated.h"

USTRUCT(BlueprintType)
struct FWarehouseNode
{
    GENERATED_BODY()
    UPROPERTY(BlueprintReadOnly) FString Id;
    UPROPERTY(BlueprintReadOnly) FVector Position = FVector::ZeroVector;
};

USTRUCT(BlueprintType)
struct FWarehouseEdge
{
    GENERATED_BODY()
    UPROPERTY(BlueprintReadOnly) FString From;
    UPROPERTY(BlueprintReadOnly) FString To;
    UPROPERTY(BlueprintReadOnly) FString Type;
    UPROPERTY(BlueprintReadOnly) TArray<FVector> Points;
};

class AAGVPathActor;

UCLASS()
class AGVSIMULATION_API AWarehouseMapLoader : public AActor
{
    GENERATED_BODY()
public:
    AWarehouseMapLoader();
    UFUNCTION(BlueprintCallable, Category = "Warehouse") bool LoadMap();
    UPROPERTY(BlueprintReadOnly, Category = "Warehouse") TArray<FWarehouseNode> Nodes;
    UPROPERTY(BlueprintReadOnly, Category = "Warehouse") TArray<FWarehouseEdge> Edges;
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Warehouse") TSubclassOf<AActor> NodeMarkerClass;
    UFUNCTION(BlueprintCallable, Category = "Warehouse") void SpawnPaths();
    UPROPERTY() TArray<TObjectPtr<AAGVPathActor>> SpawnedPaths;

    // Edge 방향을 양방향으로 해석한다. 단방향 통행은 추후 데이터 확장 필요.
    bool FindShortestRoute(const FString& StartId, const FString& DestinationId,
        TArray<int32>& OutEdgeIndices, TArray<bool>& OutReversed) const;
protected:
    virtual void BeginPlay() override;
};